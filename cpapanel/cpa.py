"""CLIProxyAPI（CPA）Management API 客户端。

设计要点（全部来自上游源码，见 docs/upstream-api.md）：

* 鉴权用 `Authorization: Bearer` 或 `X-Management-Key`，两个都发。
* **v0 与 v8 是两套路径**，本客户端用一张表把「业务操作」映射到具体前缀，
  并在首次连接时自动探测（`detect_prefix`）。
* **404 有特殊含义**：上游在「未配置管理密钥」时根本不注册管理路由，
  所以 404 代表「上游没开管理 API」，必须和普通 404 区分开，否则排障会跑偏。
* 用量队列是**消费型**（pop），因此 `usage_queue()` 有副作用，调用方必须落库。
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .log import get
from .util import jdump, jload, short

log = get("cpapanel.cpa")

# --------------------------------------------------------------------------- 路径表
# key -> {v0: path, v8: path}；None 表示该前缀下没有等价路径

PATHS: Dict[str, Dict[str, Optional[str]]] = {
    "config":            {"v0": "/config", "v8": "/config"},
    "config_yaml":       {"v0": "/config.yaml", "v8": "/config.yaml"},
    "auth_files":        {"v0": "/auth-files", "v8": "/credentials"},
    "auth_files_models": {"v0": "/auth-files/models", "v8": "/credentials/models"},
    "auth_files_download": {"v0": "/auth-files/download", "v8": "/credentials/download"},
    "auth_files_status": {"v0": "/auth-files/status", "v8": "/credentials/status"},
    "auth_files_fields": {"v0": "/auth-files/fields", "v8": "/credentials/fields"},
    "auth_files_refresh": {"v0": "/auth-files/refresh", "v8": "/credentials/refresh"},
    "usage_queue":       {"v0": "/usage-queue", "v8": "/observability/usage/queue"},
    "api_key_usage":     {"v0": "/api-key-usage", "v8": "/observability/usage/api-keys"},
    "logs":              {"v0": "/logs", "v8": "/observability/logs"},
    "logs_errors":       {"v0": "/request-error-logs", "v8": "/observability/logs/errors"},
    "request_log_by_id": {"v0": "/request-log-by-id/", "v8": "/observability/logs/requests/"},
    "api_keys":          {"v0": "/api-keys", "v8": "/config/access/api-keys"},
    "latest_version":    {"v0": "/latest-version", "v8": "/server/latest-version"},
    "usage_stats_flag":  {"v0": "/usage-statistics-enabled", "v8": None},
    "quota_providers":   {"v0": "/quota/providers", "v8": None},
    "quota_fetch":       {"v0": "/quota/fetch", "v8": None},
    "quota_reset":       {"v0": "/quota/reset", "v8": None},
    "plugins":           {"v0": "/plugins", "v8": "/plugins"},
    "oauth_auth_url":    {"v0": None, "v8": "/oauth/auth-url"},
    "oauth_status":      {"v0": "/get-auth-status", "v8": "/oauth/status"},
    "oauth_session":     {"v0": "/oauth-session", "v8": "/oauth/session"},
}

V0_PROVIDER_AUTH_URLS = {
    "claude": "/anthropic-auth-url",
    "codex": "/codex-auth-url",
    "antigravity": "/antigravity-auth-url",
    "kimi": "/kimi-auth-url",
    "kimi-ai": "/kimi-ai-auth-url",
    "xai": "/xai-auth-url",
    "devin": "/devin-auth-url",
    "meta": "/meta-auth-url",
}

OAUTH_PROVIDERS = ("claude", "codex", "antigravity", "kimi", "kimi-ai", "xai", "devin", "meta")


class CPAError(Exception):
    """统一的客户端错误：带上方法/路径/状态码，便于面板显示与排障。"""

    def __init__(self, message: str, status: Optional[int] = None, method: str = "",
                 path: str = "", body: str = ""):
        super().__init__(message)
        self.status = status
        self.method = method
        self.path = path
        self.body = body

    @property
    def management_unavailable(self) -> bool:
        """上游未启用管理 API（404）—— 这是配置问题，不是路径写错。"""
        return self.status == 404

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": str(self),
            "status": self.status,
            "method": self.method,
            "path": self.path,
            "management_unavailable": self.management_unavailable,
            "hint": ("上游未启用 Management API（通常是没设置管理密钥）"
                     if self.management_unavailable else ""),
        }


class CPAClient:
    def __init__(self, base_url: str, management_key: str = "", prefix: str = "auto",
                 timeout: Tuple[float, float] = (5, 20), verify_tls: bool = True):
        self.base_url = (base_url or "").rstrip("/")
        self.management_key = management_key or ""
        self.prefix = prefix if prefix in ("v0", "v8") else "auto"
        self.timeout = timeout
        self.verify_tls = verify_tls
        self._resolved_prefix: Optional[str] = None if self.prefix == "auto" else self.prefix
        if not self.base_url:
            raise ValueError("base_url 不能为空")

    # ------------------------------------------------------------------ 底层请求

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return self.base_url + (path if path.startswith("/") else "/" + path)

    def request(self, method: str, path: str, body: Any = None, params: Optional[Dict[str, Any]] = None,
                raw_body: Optional[bytes] = None, content_type: Optional[str] = None,
                accept: str = "application/json", timeout: Optional[Tuple[float, float]] = None,
                allow_404: bool = False) -> Tuple[int, bytes, Dict[str, str]]:
        url = self._url(path)
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean, doseq=True)

        data: Optional[bytes] = None
        headers = {"Accept": accept, "User-Agent": "cpa-panel/0.1"}
        if self.management_key:
            headers["Authorization"] = f"Bearer {self.management_key}"
            headers["X-Management-Key"] = self.management_key
        if raw_body is not None:
            data = raw_body
            if content_type:
                headers["Content-Type"] = content_type
        elif body is not None:
            data = jdump(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        context = None
        if url.startswith("https") and not self.verify_tls:
            context = ssl._create_unverified_context()  # noqa: SLF001 - 显式在配置里放开时才用
        # 注意：urllib 不接受 (connect, read) 元组超时，这里取读超时兜底；
        # 连接超时由 socket 层的默认行为控制（面板侧 http.connect_timeout 仍用于文档与后续扩展）。
        effective_timeout = timeout or self.timeout
        if isinstance(effective_timeout, (tuple, list)):
            effective_timeout = float(effective_timeout[-1])
        try:
            with urllib.request.urlopen(req, timeout=effective_timeout, context=context) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            payload = b""
            try:
                payload = exc.read()
            except Exception:  # pragma: no cover - 读取失败不重要
                pass
            if allow_404 and exc.code == 404:
                return 404, payload, dict(exc.headers or {})
            raise CPAError(
                f"HTTP {exc.code} {method.upper()} {path}: {short(payload.decode('utf-8', 'replace'), 240)}",
                status=exc.code, method=method.upper(), path=path,
                body=payload.decode("utf-8", "replace")) from None
        except urllib.error.URLError as exc:
            raise CPAError(f"连接失败 {method.upper()} {path}: {exc.reason}", method=method.upper(),
                           path=path) from None
        except (socket.timeout, TimeoutError):
            raise CPAError(f"超时 {method.upper()} {path}", method=method.upper(), path=path) from None

    def json_request(self, method: str, path: str, **kwargs: Any) -> Any:
        status, payload, _headers = self.request(method, path, **kwargs)
        if not payload:
            return None
        return jload(payload, None)

    # ------------------------------------------------------------------ 前缀解析

    def resolve_prefix(self, force: bool = False) -> str:
        """探测上游支持 v8 还是只有 v0。结果缓存到实例上。"""
        if self._resolved_prefix and not force:
            return self._resolved_prefix
        if self.prefix in ("v0", "v8"):
            self._resolved_prefix = self.prefix
            return self.prefix
        # 先试 v8（新核心推荐），再退 v0
        for candidate in ("v8", "v0"):
            path = f"/{candidate}/management{self._path('config', candidate)}"
            try:
                status, _payload, _h = self.request("GET", path, allow_404=True)
                if status == 200:
                    self._resolved_prefix = candidate
                    return candidate
                if status == 404:
                    continue  # 可能是「没开管理 API」，也可能是这个前缀不存在
            except CPAError as exc:
                if exc.status == 401:
                    # 路径存在但密钥不对 —— 说明前缀是对的，问题在密钥
                    self._resolved_prefix = candidate
                    raise CPAError("管理密钥无效（401）", status=401, method="GET", path=path)
                if exc.status == 404:
                    continue
                raise
        # 两个前缀都 404：上游没开管理 API
        self._resolved_prefix = "v0"
        raise CPAError("上游未启用 Management API（/v0 与 /v8 均返回 404；请检查管理密钥配置）",
                       status=404, method="GET", path="/v0/management/config")

    def _path(self, op: str, prefix: Optional[str] = None) -> str:
        table = PATHS.get(op)
        if table is None:
            raise KeyError(f"未定义的操作为 {op!r}")
        chosen = prefix or self._resolved_prefix or ("v8" if self.prefix == "auto" else self.prefix)
        path = table.get(chosen) or table.get("v0") or table.get("v8")
        if path is None:
            raise CPAError(f"操作 {op} 在当前前缀（{chosen}）下不可用", status=501)
        return path

    def url_for(self, op: str, prefix: Optional[str] = None) -> str:
        chosen = prefix or self._resolved_prefix or "v0"
        return f"/{chosen}/management{self._path(op, chosen)}"

    def api(self, method: str, op: str, *, path_suffix: str = "", **kwargs: Any) -> Any:
        prefix = self.resolve_prefix()
        return self.json_request(method, self.url_for(op, prefix) + path_suffix, **kwargs)

    def api_raw(self, method: str, op: str, *, path_suffix: str = "", **kwargs: Any) -> bytes:
        prefix = self.resolve_prefix()
        _status, payload, _headers = self.request(method, self.url_for(op, prefix) + path_suffix, **kwargs)
        return payload

    # ------------------------------------------------------------------ 配置

    def get_config(self) -> Dict[str, Any]:
        return self.api("GET", "config") or {}

    def get_config_yaml(self) -> str:
        payload = self.api_raw("GET", "config_yaml", accept="application/yaml, text/plain, */*")
        return payload.decode("utf-8", "replace")

    def put_config_yaml(self, text: str) -> Any:
        return self.api("PUT", "config_yaml", raw_body=text.encode("utf-8"),
                        content_type="application/yaml")

    def patch_config(self, patch: Dict[str, Any]) -> Any:
        return self.api("PATCH", "config", body=patch)

    # ------------------------------------------------------------------ 凭证

    def list_auth_files(self, name: str = "", auth_index: str = "",
                        page: Optional[int] = None, page_size: Optional[int] = None) -> Dict[str, Any]:
        """返回上游原始响应：`{observed_at, files:[...]}`，分页时带 total/has_more。"""
        params = {"name": name, "auth_index": auth_index,
                  "page": page, "page_size": page_size}
        data = self.api("GET", "auth_files", params=params)
        if isinstance(data, list):        # 某些版本直接返回数组
            return {"files": data, "observed_at": None}
        if isinstance(data, dict):
            files = data.get("files")
            if files is None and isinstance(data.get("data"), list):
                files = data["data"]
            return {"files": files or [], "observed_at": data.get("observed_at"),
                    "total": data.get("total"), "has_more": data.get("has_more")}
        return {"files": []}

    def upload_auth_file(self, filename: str, content: bytes) -> Any:
        """multipart 上传 `.json` 凭证（上游会校验扩展名与文件名安全）。"""
        boundary = "----cpapanel" + uuid.uuid4().hex
        safe_name = filename.replace("/", "_").replace("\\", "_")
        if not safe_name.lower().endswith(".json"):
            safe_name += ".json"
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'.encode(),
            b"Content-Type: application/json\r\n\r\n",
            content, b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])
        return self.api("POST", "auth_files", raw_body=body,
                        content_type=f"multipart/form-data; boundary={boundary}")

    def delete_auth_file(self, name: str, auth_index: str = "") -> Any:
        return self.api("DELETE", "auth_files", params={"name": name, "auth_index": auth_index or None})

    def patch_auth_file_status(self, name: str, disabled: bool, auth_index: str = "") -> Any:
        return self.api("PATCH", "auth_files_status",
                        body={"name": name, "auth_index": auth_index or None, "disabled": bool(disabled)})

    def patch_auth_file_fields(self, name: str, fields: Dict[str, Any], auth_index: str = "") -> Any:
        body = {"name": name, "auth_index": auth_index or None}
        body.update(fields or {})
        return self.api("PATCH", "auth_files_fields", body=body)

    def refresh_auth_files(self, all_: bool = False, name: str = "", auth_index: str = "") -> Any:
        body: Dict[str, Any] = {}
        if all_:
            body["all"] = True
        else:
            body["name"] = name
            if auth_index:
                body["auth_index"] = auth_index
        return self.api("POST", "auth_files_refresh", body=body)

    def download_auth_file(self, name: str, auth_index: str = "") -> bytes:
        return self.api_raw("GET", "auth_files_download",
                            params={"name": name, "auth_index": auth_index or None})

    def auth_file_models(self, name: str) -> List[Dict[str, Any]]:
        data = self.api("GET", "auth_files_models", params={"name": name})
        if isinstance(data, dict):
            return data.get("models") or []
        return data or []

    # ------------------------------------------------------------------ 用量

    def usage_queue(self, count: int = 50) -> List[Any]:
        """★ 消费型读取：返回的记录会从上游队列中移除。

        上游 `GetUsageQueue` 调用 `redisqueue.PopOldest(count)`，
        且队列仅保留 60 秒 —— 调用方必须立即落库。
        """
        data = self.api("GET", "usage_queue", params={"count": max(1, int(count))})
        if data is None:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("records", "items", "usage", "data"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    def api_key_usage(self) -> Any:
        return self.api("GET", "api_key_usage")

    # ------------------------------------------------------------------ 下游 Key

    def get_api_keys(self) -> List[str]:
        data = self.api("GET", "api_keys")
        return _extract_key_list(data)

    def put_api_keys(self, keys: List[str]) -> Any:
        return self.api("PUT", "api_keys", body={"api-keys": list(keys)})

    def delete_api_keys(self, keys: List[str]) -> Any:
        return self.api("DELETE", "api_keys", body={"api-keys": list(keys)})

    # ------------------------------------------------------------------ 日志

    def get_logs(self, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.api("GET", "logs", params=params)

    def delete_logs(self) -> Any:
        return self.api("DELETE", "logs")

    def error_logs(self) -> Any:
        return self.api("GET", "logs_errors")

    def download_error_log(self, name: str) -> bytes:
        return self.api_raw("GET", "logs_errors", path_suffix="/" + urllib.parse.quote(name))

    def request_log_by_id(self, request_id: str) -> Any:
        return self.api("GET", "request_log_by_id", path_suffix=urllib.parse.quote(str(request_id)))

    # ------------------------------------------------------------------ 配额

    def quota_providers(self) -> Any:
        return self.api("GET", "quota_providers")

    def fetch_quota(self, name: str = "", auth_index: str = "") -> Any:
        body: Dict[str, Any] = {}
        if name:
            body["name"] = name
        if auth_index:
            body["auth_index"] = auth_index
        return self.api("POST", "quota_fetch", body=body or None)

    def reset_quota(self, name: str = "", auth_index: str = "") -> Any:
        body: Dict[str, Any] = {}
        if name:
            body["name"] = name
        if auth_index:
            body["auth_index"] = auth_index
        return self.api("POST", "quota_reset", body=body or None)

    # ------------------------------------------------------------------ 运行参数

    def get_usage_stats_enabled(self) -> Optional[bool]:
        try:
            data = self.api("GET", "usage_stats_flag")
        except CPAError:
            return None
        if isinstance(data, dict):
            for key in ("usage-statistics-enabled", "usage_statistics_enabled", "enabled"):
                if key in data:
                    return bool(data[key])
        if isinstance(data, bool):
            return data
        return None

    def set_usage_stats_enabled(self, enabled: bool) -> Any:
        return self.api("PUT", "usage_stats_flag", body={"usage-statistics-enabled": bool(enabled)})

    def latest_version(self) -> Any:
        return self.api("GET", "latest_version")

    def plugins(self) -> Any:
        return self.api("GET", "plugins")

    # ------------------------------------------------------------------ OAuth

    def start_oauth(self, provider: str, **extra: Any) -> Dict[str, Any]:
        """发起登录，返回 `{url, state, provider}`。v8 用统一端点，v0 走 provider 专用端点。"""
        provider = (provider or "").strip().lower()
        if not provider:
            raise CPAError("provider 不能为空")
        prefix = self.resolve_prefix()
        if prefix == "v8":
            data = self.json_request("GET", f"/v8/management/oauth/auth-url", params={"provider": provider, **extra})
        else:
            legacy = V0_PROVIDER_AUTH_URLS.get(provider)
            if not legacy:
                raise CPAError(f"v0 前缀不支持 provider={provider}（请升级核心以使用 v8 OAuth）")
            data = self.json_request("GET", f"/v0/management{legacy}", params=extra)
        return _normalize_oauth_start(provider, data)

    def oauth_status(self, state: str, provider: str = "") -> Dict[str, Any]:
        prefix = self.resolve_prefix()
        if prefix == "v8":
            data = self.json_request("GET", "/v8/management/oauth/status", params={"state": state})
        else:
            data = self.json_request("GET", "/v0/management/get-auth-status",
                                     params={"state": state, "provider": provider or None})
        return data if isinstance(data, dict) else {"status": "unknown", "raw": data}

    def cancel_oauth(self, state: str) -> Any:
        prefix = self.resolve_prefix()
        if prefix == "v8":
            return self.json_request("DELETE", "/v8/management/oauth/session", params={"state": state})
        return self.json_request("DELETE", "/v0/management/oauth-session", params={"state": state})

    # ------------------------------------------------------------------ 健康

    def probe(self) -> Dict[str, Any]:
        """连接自检：前缀、版本、管理 API 可用性、用量统计开关。"""
        result: Dict[str, Any] = {"ok": False, "base_url": self.base_url}
        try:
            prefix = self.resolve_prefix(force=True)
            result["prefix"] = prefix
            result["management_url"] = f"{self.base_url}/{prefix}/management"
            config = self.get_config()
            result["ok"] = True
            if isinstance(config, dict):
                result["version"] = str(config.get("version") or config.get("Version") or "")
            flag = self.get_usage_stats_enabled()
            if flag is None and isinstance(config, dict):
                # v8 不再单独提供 usage-statistics-enabled 端点，值在 config 里
                for key in ("usage-statistics-enabled", "usage_statistics_enabled"):
                    if key in config:
                        flag = bool(config[key])
                        break
            result["usage_statistics_enabled"] = flag
            if flag is False:
                result["warning"] = "上游 usage-statistics-enabled = false，用量队列不会产生记录"
            return result
        except CPAError as exc:
            result["error"] = str(exc)
            result["status"] = exc.status
            result["management_unavailable"] = exc.management_unavailable
            if exc.management_unavailable:
                result["hint"] = "上游未启用 Management API：请在 CPA 配置里设置管理密钥并重启"
            elif exc.status == 401:
                result["hint"] = "管理密钥无效：请核对节点配置"
            return result


def _extract_key_list(data: Any) -> List[str]:
    if isinstance(data, list):
        return [str(x) for x in data if isinstance(x, (str, int))]
    if isinstance(data, dict):
        for key in ("api-keys", "api_keys", "keys", "access", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [str(x) for x in value if isinstance(x, (str, int))]
            if isinstance(value, dict) and isinstance(value.get("api-keys"), list):
                return [str(x) for x in value["api-keys"]]
    return []


def _normalize_oauth_start(provider: str, data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {"provider": provider, "url": str(data or ""), "state": ""}
    url = ""
    for key in ("url", "auth_url", "authUrl", "authorize_url", "authorization_url", "link"):
        if data.get(key):
            url = str(data[key])
            break
    state = ""
    for key in ("state", "session", "session_id", "id"):
        if data.get(key):
            state = str(data[key])
            break
    if not state and url:
        try:
            query = urllib.parse.urlparse(url).query
            state = urllib.parse.parse_qs(query).get("state", [""])[0]
        except ValueError:
            state = ""
    return {"provider": provider, "url": url, "state": state, "raw": data}
