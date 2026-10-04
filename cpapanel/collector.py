"""用量采集器。

这是整个面板最容易被做错的地方，因为上游的语义很反直觉：

* `GET .../usage/queue?count=N` 是**消费型**读取（pop），取走即消失；
* 队列只保留 **60 秒**（`redisqueue.defaultRetentionSeconds`），过期记录直接被丢掉；
* 因此采集间隔必须远小于 60 秒，而且每一条取出来的记录都必须**立刻**落库。

采集器据此实现：
1. 固定间隔轮询（默认 15s），每次尽量多取（默认 50 条）；
2. 幂等写入（去重键优先用记录自带的 request id，否则用原始 JSON 的 SHA-1）；
3. 同时增量聚合到 `usage_daily`，并标注「采集间隙」——一旦两次成功采集的间隔
   超过保留窗口（默认 60s×3），说明中间**很可能丢了数据**，写审计并告警。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

from .cpa import CPAClient, CPAError
from .log import get, note
from .models import normalize_usage_record, quality_report
from .util import day_of, jdump, now_ts, short

log = get("cpapanel.collector")


class CollectorState:
    """每个节点的采集状态（内存态，重启后从 setting 恢复最后成功时间）。"""

    def __init__(self) -> None:
        self.last_success_ts: Optional[int] = None
        self.last_attempt_ts: Optional[int] = None
        self.last_error: str = ""
        self.consecutive_errors: int = 0
        self.loops: int = 0
        self.records: int = 0
        self.duplicates: int = 0
        self.cost_usd: float = 0.0
        self.gap_warned_at: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "last_success_ts": self.last_success_ts,
            "last_attempt_ts": self.last_attempt_ts,
            "last_error": self.last_error,
            "consecutive_errors": self.consecutive_errors,
            "loops": self.loops,
            "records": self.records,
            "duplicates": self.duplicates,
            "cost_usd": round(self.cost_usd, 6),
            "gap_warned_at": self.gap_warned_at,
        }


class UsageCollector:
    def __init__(self, store: Any, config: Any, pricing: Any, notifier: Any):
        self.store = store
        self.config = config
        self.pricing = pricing
        self.notifier = notifier
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self.state: Dict[int, CollectorState] = {}
        self.usage_stats_warned: Dict[int, bool] = {}

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if not self.config.get("collector.enabled", True):
            log.info("采集器未启用（collector.enabled=false）")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="usage-collector", daemon=True)
        self._thread.start()
        log.info("采集器已启动：间隔 %ss，批量 %s 条（上游队列保留窗口 %ss）",
                 self.config.get("collector.interval_seconds"),
                 self.config.get("collector.batch_size"),
                 self.config.get("collector.queue_retention_seconds"))

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def ensure_running(self) -> None:
        """配置热更新后调用：按新配置重启采集线程。"""
        self.stop()
        self.start()

    # ------------------------------------------------------------------ 主循环

    def _loop(self) -> None:
        interval = max(3, int(self.config.get("collector.interval_seconds") or 15))
        while not self._stop.is_set():
            try:
                self.collect_all()
            except Exception as exc:  # noqa: BLE001 - 单轮失败不能让线程死掉
                log.exception("采集轮次异常：%s", exc)
            self._stop.wait(interval)

    def collect_all(self) -> Dict[str, Any]:
        results = []
        for node in self.store.list_nodes(only_enabled=True):
            try:
                results.append(self.collect_node(int(node["id"])))
            except Exception as exc:  # noqa: BLE001
                log.warning("节点 %s 采集失败：%s", node["name"], exc)
                results.append({"node_id": node["id"], "ok": False, "error": str(exc)})
        return {"nodes": results, "ts": now_ts()}

    # ------------------------------------------------------------------ 单节点

    def client_for(self, node: Any) -> CPAClient:
        return CPAClient(
            base_url=node["base_url"],
            management_key=node["management_key"],
            prefix=node["api_prefix"] if node["api_prefix"] in ("v0", "v8") else "auto",
            timeout=(float(self.config.get("http.connect_timeout") or 5),
                     float(self.config.get("http.read_timeout") or 20)),
            verify_tls=bool(self.config.get("http.verify_tls", True)),
        )

    def collect_node(self, node_id: int) -> Dict[str, Any]:
        node = self.store.get_node(node_id)
        if not node:
            return {"node_id": node_id, "ok": False, "error": "节点不存在"}
        with self._lock:
            st = self.state.setdefault(node_id, CollectorState())
        st.loops += 1
        st.last_attempt_ts = now_ts()

        batch = max(1, int(self.config.get("collector.batch_size") or 50))
        client = self.client_for(node)

        # 周期性检查上游是否关闭了用量统计（否则会「静默无数据」，非常难排查）
        if st.loops == 1 or st.loops % 40 == 0:
            self._check_usage_flag(node, client)

        try:
            records = client.usage_queue(count=batch)
        except CPAError as exc:
            st.last_error = str(exc)
            st.consecutive_errors += 1
            self.store.update_node(node_id, last_error=short(str(exc), 500))
            self.store.audit("collector.error", actor="collector", target=str(node["name"]),
                             detail=short(str(exc), 300))
            if st.consecutive_errors in (1, 3, 10):
                self.notifier.notify("collector.error", f"采集失败：{node['name']}",
                                     f"连续 {st.consecutive_errors} 次失败：{exc}")
            return {"node_id": node_id, "ok": False, "error": str(exc)}

        now = now_ts()
        gap = self._gap_check(node, st, now)

        inserted = duplicates = errors = 0
        quality_events: List[Dict[str, Any]] = []
        for raw in records:
            event = normalize_usage_record(raw, node_id)
            if event is None:
                continue
            event["cost_usd"] = self.pricing.estimate(
                event.get("model"), event.get("input_tokens", 0), event.get("output_tokens", 0),
                event.get("reasoning_tokens", 0), event.get("cached_tokens", 0))
            if self.store.insert_usage_event(node_id, event):
                self.store.aggregate_event(dict(event, _node_id=node_id))
                inserted += 1
                st.cost_usd += float(event.get("cost_usd") or 0.0)
            else:
                duplicates += 1
            if event.get("is_error"):
                errors += 1
            quality_events.append(event)

        st.records += inserted
        st.duplicates += duplicates
        st.last_success_ts = now
        st.last_error = ""
        st.consecutive_errors = 0
        self.store.update_node(node_id, last_ok_at=now, last_error="")
        self.store.set_setting(f"collector.last_success.{node_id}", now)

        # 下游 Key 用量：低频刷新
        if st.loops % max(1, int(self.config.get("collector.api_key_usage_every") or 40)) == 0:
            self._refresh_key_usage(node, client)

        if st.loops % 20 == 0:
            self.store.prune(usage_days=180, sample_days=30, audit_days=90)

        quality = quality_report(quality_events) if quality_events else {}
        if records:
            note("info", "采集完成", node=node["name"], fetched=len(records),
                 inserted=inserted, duplicates=duplicates, errors=errors)
        return {
            "node_id": node_id, "ok": True, "fetched": len(records), "inserted": inserted,
            "duplicates": duplicates, "errors": errors, "gap": gap, "quality": quality,
        }

    def _check_usage_flag(self, node: Any, client: CPAClient) -> None:
        try:
            flag = client.get_usage_stats_enabled()
        except CPAError:
            return
        if flag is False:
            if not self.usage_stats_warned.get(int(node["id"])):
                self.usage_stats_warned[int(node["id"])] = True
                self.store.audit("collector.usage_stats_disabled", actor="collector",
                                 target=str(node["name"]),
                                 detail="上游 usage-statistics-enabled=false，用量队列不会产出记录")
                self.notifier.notify(
                    "collector.error", f"上游关闭了用量统计：{node['name']}",
                    "CLIProxyAPI 的 usage-statistics-enabled 为 false，面板将采不到任何用量数据。")
        elif flag is True:
            self.usage_stats_warned[int(node["id"])] = False

    def _gap_check(self, node: Any, st: CollectorState, now: int) -> Optional[Dict[str, Any]]:
        """检测采集间隙是否超出上游保留窗口 —— 超出就意味着「有记录被静默丢弃」。"""
        retention = int(self.config.get("collector.queue_retention_seconds") or 60)
        ratio = float(self.config.get("collector.gap_warn_ratio") or 3.0)
        previous = st.last_success_ts or self.store.get_setting(f"collector.last_success.{node['id']}")
        if not previous:
            return None
        delta = now - int(previous)
        if delta <= retention * ratio:
            return None
        # 同一段间隙只报一次
        if st.gap_warned_at and now - st.gap_warned_at < max(retention * ratio, 300):
            return {"seconds": delta, "warned": False}
        st.gap_warned_at = now
        detail = (f"距上次成功采集 {delta}s，超过上游保留窗口 {retention}s×{ratio}；"
                  f"期间产生的用量记录可能已被上游丢弃（队列为消费型 + 60s 过期）")
        self.store.audit("collector.gap", actor="collector", target=str(node["name"]), detail=detail)
        self.notifier.notify("collector.gap", f"采集间隙过大：{node['name']}", detail)
        log.warning("%s: %s", node["name"], detail)
        return {"seconds": delta, "warned": True, "detail": detail}

    def _refresh_key_usage(self, node: Any, client: CPAClient) -> None:
        try:
            data = client.api_key_usage()
        except CPAError as exc:
            log.debug("节点 %s 的 Key 用量拉取失败：%s", node["name"], exc)
            return
        items = _extract_key_usage(data)
        if items:
            n = self.store.upsert_key_usage(int(node["id"]), items)
            log.debug("节点 %s 更新了 %d 条 Key 用量", node["name"], n)
        try:
            keys = client.get_api_keys()
            if keys:
                self.store.sync_api_keys(int(node["id"]), keys)
        except CPAError:
            pass

    # ------------------------------------------------------------------ 手动触发

    def collect_once(self, node_id: Optional[int] = None) -> Dict[str, Any]:
        """供 Web API / CLI 立即采集一次（不影响后台线程节奏）。"""
        if node_id:
            return self.collect_node(int(node_id))
        return self.collect_all()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {str(k): v.to_dict() for k, v in self.state.items()}


def _extract_key_usage(data: Any) -> List[Dict[str, Any]]:
    """把上游 api-key-usage 的返回摊平成统一结构（字段名宽容匹配）。"""
    rows: List[Any] = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("usage", "data", "items", "keys", "api-keys"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
        else:
            # {key: {...}} 形式
            for key, value in data.items():
                if isinstance(value, dict):
                    rows.append(dict(value, api_key=key))
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        from .util import as_int, first
        key = first(row, "api_key", "key", "name", "id", "token")
        if not key:
            continue
        out.append({
            "api_key": str(key),
            "requests": as_int(first(row, "requests", "count", "total_requests", "calls")) or 0,
            "input_tokens": as_int(first(row, "input_tokens", "prompt_tokens")) or 0,
            "output_tokens": as_int(first(row, "output_tokens", "completion_tokens")) or 0,
            "cost_usd": float(first(row, "cost_usd", "cost") or 0.0),
            "raw": row,
        })
    return out
