"""把上游的原始数据翻译成面板的规范模型。

这里做两件事：

1. **凭证状态机** —— 上游只给 `status/disabled/unavailable/next_retry_after/quota.signals`
   这些原始信号，运维真正想知道的是「这个号是死了要重登、还是在冷却、还是被禁用了」。
   `classify_credential()` 把原始信号收敛成 5 个可操作状态，并保留判定依据（evidence）。

2. **用量记录归一化** —— 上游把队列记录当不透明 JSON 转发，字段名没有权威 schema。
   因此这里用别名表做**尽力而为**的归一化，同时保证**原始 JSON 一定被保留**。
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

from .util import as_bool, as_float, as_int, first, jdump, parse_ts

# --------------------------------------------------------------------------- 状态

STATE_HEALTHY = "healthy"
STATE_COOLING = "cooling"            # 冷却中（额度窗口/限流），会自己恢复
STATE_QUOTA_EXHAUSTED = "quota_exhausted"
STATE_UNAUTHORIZED = "unauthorized"  # 真实失效，必须重新登录
STATE_DISABLED = "disabled"
STATE_UNKNOWN = "unknown"

UNAUTHORIZED_HINTS = (
    "token expired", "expired", "invalid_grant", "unauthorized", "401",
    "invalid api key", "authentication", "re-authenticate", "login required",
    "refresh token", "revoked", "account deactivated",
)
QUOTA_HINTS = ("quota", "rate limit", "rate_limit", "limit reached", "insufficient",
               "exhausted", "capacity", "usage limit", "429", "too many requests")


def classify_credential(entry: Dict[str, Any], now: Optional[int] = None) -> Dict[str, Any]:
    """返回 {state, reason, evidence, recoverable}。

    判定顺序刻意设计成：禁用 > 失效 > 额度 > 冷却 > 健康，
    因为「已被人工禁用」和「需要重登」的处置动作完全不同。
    """
    import time
    now = now or int(time.time())

    disabled = as_bool(entry.get("disabled")) or str(entry.get("status") or "").lower() == "disabled"
    if disabled:
        return {"state": STATE_DISABLED, "reason": entry.get("status_message") or "上游标记为禁用",
                "evidence": {"disabled": True}, "recoverable": True}

    status = str(entry.get("status") or "").lower()
    message = str(entry.get("status_message") or "")
    lowered = message.lower()

    # 1) 明确的认证失效
    if any(hint in lowered for hint in UNAUTHORIZED_HINTS):
        return {"state": STATE_UNAUTHORIZED, "reason": message or "认证失败",
                "evidence": {"status_message": message}, "recoverable": False}

    # 2) 订阅/token 已过期（Codex 的 id_token 会带订阅有效期）
    id_token = entry.get("id_token") if isinstance(entry.get("id_token"), dict) else {}
    until = parse_ts(first(id_token, "chatgpt_subscription_active_until"))
    if until and until < now:
        return {"state": STATE_UNAUTHORIZED, "reason": "订阅已过期（id_token 有效期结束）",
                "evidence": {"subscription_until": until}, "recoverable": False}

    # 3) 额度耗尽（上游的被动观测 signals / 文本提示）
    signals = _quota_signals(entry)
    quota_hit = [name for name, value in signals.items()
                 if any(hint in name.lower() for hint in QUOTA_HINTS) and _truthy_signal(value)]
    if any(hint in lowered for hint in QUOTA_HINTS):
        quota_hit.append("status_message")
    if quota_hit:
        # 注意：quota_hit 里可能有 "status_message" 这类「证据来源」，它不在 signals 字典里，
        # 所以取 evidence 时必须过滤，否则会 KeyError（这个坑实测踩过）。
        evidence = {k: signals[k] for k in sorted(set(quota_hit))[:6] if k in signals}
        return {"state": STATE_QUOTA_EXHAUSTED,
                "reason": message or "额度/限流已耗尽：" + ", ".join(sorted(set(quota_hit))[:3]),
                "evidence": {"signals": evidence, "matched_via": sorted(set(quota_hit))[:6]},
                "recoverable": True}

    # 4) 冷却中（unavailable + 未来时间点）
    next_retry = parse_ts(entry.get("next_retry_after")) or parse_ts(entry.get("nextRetryAfter"))
    unavailable = as_bool(entry.get("unavailable"))
    if unavailable and next_retry and next_retry > now:
        return {"state": STATE_COOLING, "reason": "冷却中，预计自动恢复",
                "evidence": {"next_retry_after": next_retry}, "recoverable": True}
    if unavailable:
        return {"state": STATE_COOLING, "reason": message or "暂不可调度",
                "evidence": {"unavailable": True}, "recoverable": True}

    # 5) 错误但原因未知
    if status == "error":
        return {"state": STATE_UNKNOWN, "reason": message or "上游标记 error（原因未知）",
                "evidence": {"status": status}, "recoverable": True}

    return {"state": STATE_HEALTHY, "reason": "", "evidence": {}, "recoverable": True}


def _quota_signals(entry: Dict[str, Any]) -> Dict[str, Any]:
    quota = entry.get("quota")
    out: Dict[str, Any] = {}
    if isinstance(quota, dict) and isinstance(quota.get("signals"), dict):
        out.update(quota["signals"])
    per_model = entry.get("model_quotas")
    if isinstance(per_model, dict):
        for model, payload in per_model.items():
            if isinstance(payload, dict) and isinstance(payload.get("signals"), dict):
                for k, v in payload["signals"].items():
                    out[f"{model}.{k}"] = v
    return out


def _truthy_signal(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    s = str(value).strip().lower()
    return s in ("true", "yes", "1", "exhausted", "exceeded", "blocked", "limited", "over")


def is_active(classified: Dict[str, Any]) -> bool:
    return classified.get("state") in (STATE_HEALTHY,)


def needs_relogin(classified: Dict[str, Any]) -> bool:
    return classified.get("state") == STATE_UNAUTHORIZED


# --------------------------------------------------------------------------- 凭证

def normalize_auth_file(entry: Dict[str, Any]) -> Dict[str, Any]:
    """上游 auth-files entry → store.credentials 的字段载荷。"""
    classified = classify_credential(entry)
    id_token = entry.get("id_token") if isinstance(entry.get("id_token"), dict) else {}
    subscription_until = parse_ts(first(id_token, "chatgpt_subscription_active_until"))
    recent = entry.get("recent_requests")
    recent_req = as_int(first(recent, "total", "count", "requests")) if isinstance(recent, dict) else as_int(recent)

    return {
        "name": str(entry.get("name") or entry.get("id") or ""),
        "auth_index": str(entry.get("auth_index") or entry.get("authIndex") or ""),
        "provider": str(entry.get("provider") or entry.get("type") or "").lower(),
        "email": entry.get("email") or "",
        "account": entry.get("account") or "",
        "account_type": entry.get("account_type") or "",
        "project_id": entry.get("project_id") or "",
        "plan_type": id_token.get("plan_type") or "",
        "status": str(entry.get("status") or ""),
        "status_message": entry.get("status_message") or classified.get("reason") or "",
        "disabled": 1 if as_bool(entry.get("disabled")) else 0,
        "unavailable": 1 if as_bool(entry.get("unavailable")) else 0,
        "runtime_only": 1 if as_bool(entry.get("runtime_only")) else 0,
        "source": entry.get("source") or "",
        "path": entry.get("path") or "",
        "priority": as_int(entry.get("priority")),
        "weight": as_int(entry.get("weight")),
        "note": entry.get("note") or "",
        "websockets": _opt_bool(entry.get("websockets")),
        "request_retry": as_int(entry.get("request_retry")),
        "last_refresh": parse_ts(first(entry, "last_refresh", "lastRefresh", "updated_at", "modtime")),
        "next_retry_after": parse_ts(entry.get("next_retry_after")),
        "subscription_until": subscription_until,
        "success": as_int(first(entry, "success", "success_count")) or 0,
        "failed": as_int(first(entry, "failed", "failed_count")) or 0,
        "recent_requests_json": jdump(recent) if recent is not None else None,
        "quota_json": jdump(entry.get("quota")) if entry.get("quota") is not None else None,
        "model_quotas_json": jdump(entry.get("model_quotas")) if entry.get("model_quotas") else None,
        "id_token_json": jdump(id_token) if id_token else None,
        "raw_json": jdump(entry),
        "_classified": classified,
        "_recent_requests": recent_req,
    }


def _opt_bool(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    return 1 if as_bool(value) else 0


def credential_view(row: Any) -> Dict[str, Any]:
    """数据库行 → Web UI 用的字典（统一把 0/1 转成布尔、补上分类结论）。"""
    item = dict(row)
    for key in ("disabled", "unavailable", "runtime_only", "standby", "present", "websockets"):
        if item.get(key) is not None:
            item[key] = bool(item[key])
    raw_quota = None
    try:
        import json
        raw_quota = json.loads(item.get("quota_json") or "null")
    except ValueError:
        raw_quota = None
    classified = classify_credential({
        "disabled": item.get("disabled"),
        "status": item.get("status"),
        "status_message": item.get("status_message"),
        "unavailable": item.get("unavailable"),
        "next_retry_after": item.get("next_retry_after"),
        "quota": raw_quota,
        "id_token": {"chatgpt_subscription_active_until": item.get("subscription_until")},
    })
    item["state"] = classified["state"]
    item["state_reason"] = classified["reason"]
    item["recoverable"] = classified["recoverable"]
    item["quota"] = raw_quota
    item.pop("raw_json", None)
    item.pop("quota_json", None)
    return item


# --------------------------------------------------------------------------- 用量
# 字段别名表：不同版本/提供方的记录字段名不一致，这里穷举常见的写法。

_ALIASES: Dict[str, tuple] = {
    "id": ("request_id", "id", "requestid", "trace_id", "traceid", "uuid", "event_id", "span_id"),
    "ts": ("timestamp", "ts", "time", "created_at", "createdat", "at", "request_time", "start_time", "date"),
    "model": ("model", "model_name", "modelname", "requested_model", "model_id", "deployment"),
    "provider": ("provider", "channel", "auth_provider", "provider_name", "upstream", "platform"),
    "credential_index": ("auth_index", "authindex", "credential", "credential_id", "auth_id",
                         "account", "auth", "auth_name"),
    "credential_label": ("credential_label", "auth_label", "account_email", "email", "label"),
    "api_key": ("api_key", "apikey", "client_key", "key", "token_id", "access_key"),
    "endpoint": ("endpoint", "path", "url", "route", "request_path"),
    "status": ("status", "state", "result"),
    "http_status": ("status_code", "statuscode", "http_status", "code", "http_code"),
    "latency_ms": ("latency_ms", "duration_ms", "elapsed_ms", "latency", "duration", "took_ms",
                   "response_time", "cost_time"),
    "input_tokens": ("input_tokens", "prompt_tokens", "tokens_in", "input_token_count",
                     "prompt_token_count", "input", "prompt"),
    "output_tokens": ("output_tokens", "completion_tokens", "tokens_out", "output_token_count",
                      "completion_token_count", "output", "completion"),
    "reasoning_tokens": ("reasoning_tokens", "reasoning_token_count", "thinking_tokens",
                         "reasoning", "thoughts_tokens"),
    "cached_tokens": ("cached_tokens", "cache_read_input_tokens", "cache_read_tokens",
                      "cached_token_count", "cache_hit_tokens", "cache_read", "cached"),
    "total_tokens": ("total_tokens", "total_token_count", "tokens", "token_count", "total"),
    "error": ("error", "error_message", "err", "reason", "exception", "failure_reason"),
}

_NESTED_SOURCES = ("usage", "tokens", "token_usage", "response", "meta", "metadata", "data")


def normalize_usage_record(raw: Any, node_id: int) -> Optional[Dict[str, Any]]:
    """把一条队列记录转成 usage_events 的行（含 dedupe_key 与成本占位）。

    * 记录可能是对象，也可能是 `{"support_refresh": true}` 之类的控制帧 —— 控制帧返回 None。
    * 归一化失败不影响落库：raw_json 始终保留。
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("support_refresh") or raw.get("refresh"):
        return None  # 控制帧，不是业务记录

    # 展开嵌套：把 usage/tokens/response 等子对象拍平到顶层参与别名匹配
    flat: Dict[str, Any] = {}
    nested_keys: List[str] = []
    for key, value in raw.items():
        if isinstance(value, dict) and key in _NESTED_SOURCES:
            nested_keys.append(key)
            flat.update(value)
    flat.update({k: v for k, v in raw.items() if not isinstance(v, dict)})

    def take(name: str, source: Dict[str, Any]) -> Any:
        return first(source, *(_ALIASES[name]))

    ts = parse_ts(take("ts", flat) or take("ts", raw)) or _now()
    input_tokens = as_int(take("input_tokens", flat) or take("input_tokens", raw)) or 0
    output_tokens = as_int(take("output_tokens", flat) or take("output_tokens", raw)) or 0
    reasoning_tokens = as_int(take("reasoning_tokens", flat) or take("reasoning_tokens", raw)) or 0
    cached_tokens = as_int(take("cached_tokens", flat) or take("cached_tokens", raw)) or 0
    total_tokens = as_int(take("total_tokens", flat) or take("total_tokens", raw)) or (
        input_tokens + output_tokens + reasoning_tokens)
    http_status = as_int(take("http_status", flat) or take("http_status", raw))
    latency_ms = _to_millis(*_latency_hit(flat, raw))
    status = take("status", flat) or take("status", raw)
    error = take("error", flat) or take("error", raw)
    is_error = bool(error) or (http_status is not None and http_status >= 400) or \
        str(status or "").lower() in ("error", "failed", "failure")

    event = {
        "node_id": node_id,
        "dedupe_key": _dedupe_key(raw, ts),
        "ts": ts,
        "model": str(take("model", flat) or take("model", raw) or "")[:120],
        "provider": str(take("provider", flat) or take("provider", raw) or "").lower()[:60],
        "credential_index": str(take("credential_index", flat) or take("credential_index", raw) or "")[:120],
        "credential_label": str(take("credential_label", flat) or take("credential_label", raw) or "")[:160],
        "api_key": str(take("api_key", flat) or take("api_key", raw) or "")[:160],
        "endpoint": str(take("endpoint", flat) or take("endpoint", raw) or "")[:200],
        "status": str(status or ("error" if is_error else "ok"))[:40],
        "http_status": http_status,
        "latency_ms": latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cached_tokens": cached_tokens,
        "total_tokens": total_tokens,
        "cost_usd": 0.0,  # 由调用方（采集器）用 Pricing 填充
        "is_error": is_error,
        "error": str(error)[:500] if error else "",
        "raw_json": jdump(raw),
        "_nested_keys": sorted(nested_keys),
        "_unmapped": sorted(set(raw.keys()) - set(_ALIASES_KEYS) - set(_NESTED_SOURCES)),
    }
    return event


_ALIASES_KEYS = {alias for aliases in _ALIASES.values() for alias in aliases}


def _alias_hit(data: Any, name: str) -> Tuple[Optional[str], Any]:
    """在 data 中按别名表取值，返回 (命中的别名, 值)。别名用于判断单位（ms 还是 秒）。"""
    if not isinstance(data, dict):
        return None, None
    normalized = {_norm(k): k for k in data}
    for alias in _ALIASES[name]:
        actual = normalized.get(_norm(alias))
        if actual is None:
            continue
        value = data[actual]
        if value is not None and value != "":
            return alias, value
    return None, None


def _latency_hit(flat: Dict[str, Any], raw: Dict[str, Any]) -> Tuple[Optional[str], Any]:
    key, value = _alias_hit(flat, "latency_ms")
    if value is None:
        key, value = _alias_hit(raw, "latency_ms")
    return key, value


def _to_millis(key: Optional[str], value: Any) -> Optional[int]:
    """统一成毫秒。

    单位无法从上游确定，所以用一条**保守且可解释**的规则：
      * 键名以 `ms` 结尾（latency_ms/duration_ms/…）→ 原样当毫秒；
      * 其余键（duration/latency/…）：值为**小于 60 的小数**时视为秒（如 2.5 → 2500ms）；
        整数一律当毫秒，避免把 500(ms) 误读成 500 秒。
    宁可不换算，也不要凭空放大 1000 倍。
    """
    numeric = as_float(value)
    if numeric is None:
        return None
    if key and key.endswith("ms"):
        return int(numeric)
    if numeric < 60 and float(numeric) != int(numeric):
        return int(round(numeric * 1000))
    return int(numeric)


def _norm(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_")


def _dedupe_key(raw: Dict[str, Any], ts: int) -> str:
    explicit = first(raw, *_ALIASES["id"])
    if explicit:
        return f"id:{explicit}"
    canonical = jdump(raw)
    return "sha1:" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()


def _now() -> int:
    import time
    return int(time.time())


def quality_report(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """采集质量自检：归一化后有多少字段是空的、raw 里有哪些没被识别的键。

    这是「承认不确定」的落地方式 —— 与其假装解析成功，不如把未知暴露出来。
    """
    total = len(events)
    if not total:
        return {"total": 0}
    missing_model = sum(1 for e in events if not e.get("model"))
    missing_cred = sum(1 for e in events if not e.get("credential_index"))
    missing_tokens = sum(1 for e in events if not (e.get("total_tokens") or e.get("input_tokens")))
    unmapped: Dict[str, int] = {}
    for e in events:
        for key in e.get("_unmapped") or []:
            unmapped[key] = unmapped.get(key, 0) + 1
    return {
        "total": total,
        "missing_model": missing_model,
        "missing_credential": missing_cred,
        "missing_tokens": missing_tokens,
        "unmapped_keys": sorted(unmapped.items(), key=lambda kv: -kv[1])[:20],
    }
