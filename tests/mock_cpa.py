"""Mock CLIProxyAPI 管理端 —— 严格复刻上游语义，用于端到端验证。

它刻意实现了几个**容易被做错的真实行为**，这样测试才能真的抓到 bug：

1. 鉴权：密钥不对 → **401**；上游**没配密钥** → 管理路由整体 **404**（不是 401）。
2. 双前缀：可配置只开 v0 / 只开 v8 / 都开，用于验证自动探测。
3. 用量队列：`count` 缺省 1，**非正整数返回 400**；读取即删除（pop）；
   超过 TTL（默认 60s）的记录**自动丢弃**。
4. 凭证列表：无分页时返回 `{observed_at, files}`；带 page/page_size 时追加 total/has_more。
5. 写入动作：PATCH status/fields、DELETE、上传（multipart）都会真实改变状态。
6. 控制面（`/__test__/*`）：只在测试里用，用于播种数据与读取内部状态。
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple


class MockState:
    def __init__(self, management_key: str = "sk-mock-key", prefix: str = "v8",
                 usage_ttl: int = 60):
        self.lock = threading.RLock()
        self.management_key = management_key
        self.prefix = prefix              # v0 | v8 | both
        self.usage_ttl = usage_ttl
        self.credentials: List[Dict[str, Any]] = []
        self.usage: List[Tuple[float, Dict[str, Any]]] = []
        self.api_keys: List[str] = []
        self.key_usage: List[Dict[str, Any]] = []
        self.logs: List[str] = []
        self.error_logs: Dict[str, str] = {}
        self.usage_stats_enabled = True
        self.oauth_sessions: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Dict[str, Any]] = []      # 记录收到的请求，便于断言
        self.config: Dict[str, Any] = {
            "port": 8317, "version": "6.9.49",
            "auth-dir": "/tmp/cpa-auth",
            "api-keys": [],
            "usage-statistics-enabled": True,
        }

    # ------------------------------------------------------------------ 内部

    def prune_usage(self) -> int:
        now = time.time()
        before = len(self.usage)
        self.usage = [(ts, payload) for ts, payload in self.usage if now - ts <= self.usage_ttl]
        return before - len(self.usage)

    def pop_usage(self, count: int) -> List[Dict[str, Any]]:
        self.prune_usage()
        taken = self.usage[:count]
        self.usage = self.usage[count:]
        return [payload for _ts, payload in taken]

    def find_credential(self, name: str = "", auth_index: str = "") -> Optional[Dict[str, Any]]:
        for item in self.credentials:
            if name and item.get("name") != name:
                continue
            if auth_index and item.get("auth_index") != auth_index:
                continue
            return item
        return None


class MockCPA:
    """生命周期封装：start() / stop() / base_url。"""

    def __init__(self, management_key: str = "sk-mock-key", prefix: str = "v8",
                 usage_ttl: int = 60):
        self.state = MockState(management_key, prefix, usage_ttl)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port = 0

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> "MockCPA":
        handler = type("MockHandler", (MockHandlerClass,), {"state": self.state})
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="mock-cpa", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        self._httpd = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "MockCPA":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ------------------------------------------------------------------ 播种

    def seed_credentials(self, items: List[Dict[str, Any]]) -> None:
        with self.state.lock:
            self.state.credentials = [dict(i) for i in items]

    def push_usage(self, *records: Dict[str, Any]) -> None:
        """写入队列（带当前时间戳）。"""
        with self.state.lock:
            now = time.time()
            for record in records:
                payload = dict(record)
                payload.setdefault("timestamp", int(now))
                self.state.usage.append((now, payload))

    def push_usage_aged(self, age_seconds: float, record: Dict[str, Any]) -> None:
        """写入一条「已经存在 age_seconds」的记录，用于验证 TTL 淘汰。"""
        with self.state.lock:
            self.state.usage.append((time.time() - age_seconds, dict(record)))

    def queue_size(self) -> int:
        with self.state.lock:
            return len(self.state.usage)

    def calls(self) -> List[Dict[str, Any]]:
        with self.state.lock:
            return list(self.state.calls)


def _sample_credential(name: str = "codex-1.json", auth_index: str = "idx-1",
                       provider: str = "codex", **overrides: Any) -> Dict[str, Any]:
    base = {
        "id": name.replace(".json", ""), "auth_index": auth_index, "name": name,
        "type": provider, "provider": provider, "label": name,
        "status": "active", "status_message": "", "disabled": False, "unavailable": False,
        "runtime_only": False, "source": "file", "size": 1024,
        "success": 10, "failed": 0, "recent_requests": {"total": 10},
        "quota": {"observed_at": int(time.time()), "signals": {}},
        "supports_quota": True, "quota_provider": provider,
        "email": f"{name}@example.com", "account_type": "plus",
        "created_at": int(time.time()) - 86400, "modtime": int(time.time()),
        "updated_at": int(time.time()), "last_refresh": int(time.time()),
        "path": f"/tmp/cpa-auth/{name}", "priority": 0, "weight": 1, "note": "",
        "websockets": False, "request_retry": None,
        "id_token": {"chatgpt_account_id": "acc", "plan_type": "plus"},
    }
    base.update(overrides)
    return base


SAMPLE_CREDENTIALS = [
    _sample_credential("codex-1.json", "idx-1"),
    _sample_credential("codex-2.json", "idx-2", status="error",
                       status_message="token expired", failed=5),
    _sample_credential("claude-1.json", "idx-3", provider="claude"),
    _sample_credential("gemini-1.json", "idx-4", provider="gemini",
                       quota={"observed_at": int(time.time()),
                              "signals": {"quota_exhausted": True}}),
    _sample_credential("codex-3.json", "idx-5", unavailable=True,
                       next_retry_after=int(time.time()) + 600),
    _sample_credential("codex-4.json", "idx-6", disabled=True, status="disabled"),
]


SAMPLE_USAGE = [
    {"request_id": "req-1", "timestamp": int(time.time()), "model": "gpt-5-codex",
     "provider": "codex", "auth_index": "idx-1", "api_key": "sk-client-aaaa",
     "status_code": 200, "latency_ms": 1200, "prompt_tokens": 1000, "completion_tokens": 500,
     "reasoning_tokens": 100, "cached_tokens": 200, "total_tokens": 1600},
    {"request_id": "req-2", "timestamp": int(time.time()), "model": "claude-sonnet-4",
     "provider": "claude", "auth_index": "idx-3", "api_key": "sk-client-bbbb",
     "status_code": 500, "error": "upstream error", "latency_ms": 300,
     "input_tokens": 800, "output_tokens": 0},
    {"request_id": "req-3", "timestamp": int(time.time()), "model": "gpt-5-codex",
     "provider": "codex", "auth_index": "idx-1", "api_key": "sk-client-aaaa",
     "usage": {"prompt_tokens": 2000, "completion_tokens": 1000}, "duration": 2.5,
     "status": "ok"},
    {"support_refresh": True},   # 控制帧：面板必须忽略
]


class MockHandlerClass(BaseHTTPRequestHandler):
    state: MockState = None  # type: ignore[assignment]
    server_version = "mock-cpa/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102
        pass

    # ------------------------------------------------------------------ 分发

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        with self.state.lock:
            self.state.calls.append({"method": method, "path": path,
                                     "query": {k: v[0] for k, v in query.items()},
                                     "auth": self.headers.get("Authorization") or "",
                                     "mgmt_key": self.headers.get("X-Management-Key") or ""})

        # 控制面（测试专用）
        if path.startswith("/__test__"):
            return self._control(method, path, body)

        match = re.match(r"^/(v0|v8)/management(?P<rest>/.*)?$", path)
        if not match:
            return self._json(404, {"error": "not found"})

        prefix = match.group(1)
        rest = match.group(2) or "/"

        if not self._prefix_enabled(prefix):
            return self._json(404, {"error": "management api not registered for this prefix"})
        if not self._authorized():
            # 上游在「没配密钥」时根本不注册路由；配了但不对则 401
            if not self.state.management_key:
                return self._json(404, {"error": "management api disabled"})
            return self._json(401, {"error": "unauthorized"})

        return self._route(prefix, method, rest.rstrip("/") or "/", query, body)

    def do_GET(self) -> None:      # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:     # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:      # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:    # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:   # noqa: N802
        self._dispatch("DELETE")

    # ------------------------------------------------------------------ 鉴权

    def _prefix_enabled(self, prefix: str) -> bool:
        configured = self.state.prefix
        return configured == "both" or configured == prefix

    def _authorized(self) -> bool:
        expected = self.state.management_key
        if not expected:
            return False
        bearer = (self.headers.get("Authorization") or "").replace("Bearer ", "").strip()
        header = (self.headers.get("X-Management-Key") or "").strip()
        return bearer == expected or header == expected

    # ------------------------------------------------------------------ 路由

    def _route(self, prefix: str, method: str, rest: str, query: Dict[str, List[str]],
               body: bytes) -> None:
        table = _ROUTES_V8 if prefix == "v8" else _ROUTES_V0
        for route_method, pattern, handler in table:
            match = pattern.match(rest)
            if not match:
                continue
            # 一条路由可以声明多个方法（如 "GET|PUT|PATCH"）
            if method not in route_method.split("|"):
                continue
            return handler(self, match.groupdict(), query, body)
        return self._json(404, {"error": f"no route for {method} {rest}"})

    # ------------------------------------------------------------------ 控制面

    def _control(self, method: str, path: str, body: bytes) -> None:
        if path == "/__test__/state":
            return self._json(200, {
                "credentials": self.state.credentials,
                "queue_size": self.state.queue_size(),
                "api_keys": self.state.api_keys,
                "usage_stats_enabled": self.state.usage_stats_enabled,
                "calls": len(self.state.calls),
                "oauth_sessions": list(self.state.oauth_sessions.keys()),
            })
        if path == "/__test__/calls":
            return self._json(200, {"calls": self.state.calls})
        if path == "/__test__/reset_calls":
            with self.state.lock:
                self.state.calls = []
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "unknown control endpoint"})

    # ------------------------------------------------------------------ 端点实现

    def _json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _text(self, status: int, text: str, ctype: str = "text/plain; charset=utf-8") -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self, body: bytes) -> Dict[str, Any]:
        if not body:
            return {}
        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    # --- config ---

    def h_config(self, params: Dict[str, str], query: Dict[str, List[str]], body: bytes) -> None:
        method = self.command
        if method == "GET":
            return self._json(200, self.state.config)
        if method in ("PUT", "PATCH"):
            patch = self._read_json(body)
            if method == "PUT":
                self.state.config = patch
            else:
                _deep_merge(self.state.config, patch)
            return self._json(200, {"ok": True})
        return self._json(405, {"error": "method not allowed"})

    def h_config_yaml(self, params: Dict[str, str], query: Dict[str, List[str]], body: bytes) -> None:
        if self.command == "GET":
            return self._text(200, "# mock config\nport: 8317\n")
        self.state.config["_yaml"] = body.decode("utf-8", "replace")
        return self._json(200, {"ok": True})

    def h_config_access_keys(self, params: Dict[str, str], query: Dict[str, List[str]],
                             body: bytes) -> None:
        if self.command == "GET":
            return self._json(200, list(self.state.api_keys))
        data = self._read_json(body)
        keys = data if isinstance(data, list) else (data.get("api-keys") or data.get("api_keys") or [])
        if self.command == "PUT":
            self.state.api_keys = [str(k) for k in keys]
        elif self.command == "DELETE":
            drop = {str(k) for k in keys}
            self.state.api_keys = [k for k in self.state.api_keys if k not in drop]
        return self._json(200, {"ok": True, "count": len(self.state.api_keys)})

    # --- credentials ---

    def h_auth_files(self, params: Dict[str, str], query: Dict[str, List[str]], body: bytes) -> None:
        method = self.command
        if method == "GET":
            name = query.get("name", [""])[0]
            auth_index = query.get("auth_index", [""])[0]
            files = list(self.state.credentials)
            if name:
                files = [f for f in files if f.get("name") == name]
            if auth_index:
                files = [f for f in files if f.get("auth_index") == auth_index]
            page = query.get("page", [""])[0]
            page_size = query.get("page_size", [""])[0]
            if page or page_size:
                size = int(page_size or 50)
                index = int(page or 1)
                start = (index - 1) * size
                payload = {
                    "observed_at": int(time.time()),
                    "files": files[start:start + size],
                    "total": len(files), "page": index, "page_size": size,
                    "has_more": start + size < len(files),
                }
                return self._json(200, payload)
            return self._json(200, {"observed_at": int(time.time()), "files": files})
        if method == "POST":
            return self._upload(body)
        if method == "DELETE":
            name = query.get("name", [""])[0]
            target = self.state.find_credential(name)
            if not target:
                return self._json(404, {"error": "not found"})
            self.state.credentials = [c for c in self.state.credentials if c is not target]
            return self._json(200, {"ok": True})
        return self._json(405, {"error": "method not allowed"})

    def _upload(self, body: bytes) -> None:
        text = body.decode("utf-8", "replace")
        filename_match = re.search(r'filename="([^"]+)"', text)
        filename = filename_match.group(1) if filename_match else "upload.json"
        if not filename.lower().endswith(".json"):
            return self._json(400, {"error": "auth file must be JSON"})
        entry = _sample_credential(filename, f"idx-{uuid.uuid4().hex[:6]}")
        self.state.credentials.append(entry)
        return self._json(200, {"ok": True, "name": filename})

    def h_auth_files_status(self, params: Dict[str, str], query: Dict[str, List[str]],
                            body: bytes) -> None:
        data = self._read_json(body)
        target = self.state.find_credential(str(data.get("name") or ""),
                                            str(data.get("auth_index") or ""))
        if not target:
            return self._json(404, {"error": "not found"})
        if "disabled" in data:
            target["disabled"] = bool(data["disabled"])
            target["status"] = "disabled" if data["disabled"] else "active"
        return self._json(200, {"ok": True})

    def h_auth_files_fields(self, params: Dict[str, str], query: Dict[str, List[str]],
                            body: bytes) -> None:
        data = self._read_json(body)
        target = self.state.find_credential(str(data.get("name") or ""),
                                            str(data.get("auth_index") or ""))
        if not target:
            return self._json(404, {"error": "not found"})
        for key in ("priority", "note", "weight", "websockets", "label"):
            if key in data:
                target[key] = data[key]
        return self._json(200, {"ok": True})

    def h_auth_files_refresh(self, params: Dict[str, str], query: Dict[str, List[str]],
                             body: bytes) -> None:
        data = self._read_json(body)
        if not data.get("all"):
            target = self.state.find_credential(str(data.get("name") or ""))
            if not target:
                return self._json(404, {"error": "not found"})
            target["last_refresh"] = int(time.time())
        return self._json(200, {"ok": True})

    def h_auth_files_download(self, params: Dict[str, str], query: Dict[str, List[str]],
                              body: bytes) -> None:
        name = query.get("name", [""])[0]
        target = self.state.find_credential(name)
        if not target:
            return self._json(404, {"error": "not found"})
        return self._text(200, json.dumps({"name": name, "mock": True}),
                          "application/json; charset=utf-8")

    def h_auth_files_models(self, params: Dict[str, str], query: Dict[str, List[str]],
                            body: bytes) -> None:
        name = query.get("name", [""])[0]
        if not name:
            return self._json(400, {"error": "name is required"})
        target = self.state.find_credential(name)
        if not target:
            return self._json(404, {"error": "not found"})
        provider = target.get("provider")
        models = {
            "codex": [{"id": "gpt-5-codex", "display_name": "GPT-5 Codex", "type": "codex",
                       "owned_by": "openai"}],
            "claude": [{"id": "claude-sonnet-4", "display_name": "Claude Sonnet 4",
                        "type": "claude", "owned_by": "anthropic"}],
            "gemini": [{"id": "gemini-2.5-pro", "display_name": "Gemini 2.5 Pro",
                        "type": "gemini", "owned_by": "google"}],
        }.get(provider, [])
        return self._json(200, {"models": models})

    # --- usage ---

    def h_usage_queue(self, params: Dict[str, str], query: Dict[str, List[str]],
                      body: bytes) -> None:
        raw = query.get("count", [""])[0]
        if raw == "":
            count = 1
        else:
            try:
                count = int(raw)
            except ValueError:
                return self._json(400, {"error": "count must be a positive integer"})
            if count <= 0:
                return self._json(400, {"error": "count must be a positive integer"})
        records = self.state.pop_usage(count)
        return self._json(200, records)

    def h_api_key_usage(self, params: Dict[str, str], query: Dict[str, List[str]],
                        body: bytes) -> None:
        return self._json(200, self.state.key_usage)

    # --- 老式 v0 key 接口 ---

    def h_api_keys(self, params: Dict[str, str], query: Dict[str, List[str]], body: bytes) -> None:
        if self.command == "GET":
            return self._json(200, {"api-keys": list(self.state.api_keys)})
        data = self._read_json(body)
        keys = data.get("api-keys") or data.get("api_keys") or []
        if self.command == "PUT":
            self.state.api_keys = [str(k) for k in keys]
            return self._json(200, {"ok": True, "count": len(self.state.api_keys)})
        if self.command == "DELETE":
            self.state.api_keys = [k for k in self.state.api_keys if k not in set(map(str, keys))]
            return self._json(200, {"ok": True, "count": len(self.state.api_keys)})
        return self._json(405, {"error": "method not allowed"})

    # --- 运行开关 ---

    def h_usage_stats_flag(self, params: Dict[str, str], query: Dict[str, List[str]],
                           body: bytes) -> None:
        if self.command == "GET":
            return self._json(200, {"usage-statistics-enabled": self.state.usage_stats_enabled})
        data = self._read_json(body)
        if "usage-statistics-enabled" in data:
            self.state.usage_stats_enabled = bool(data["usage-statistics-enabled"])
        return self._json(200, {"ok": True})

    def h_latest_version(self, params: Dict[str, str], query: Dict[str, List[str]],
                         body: bytes) -> None:
        return self._json(200, {"version": "6.9.49", "latest": "6.9.49", "update_available": False})

    def h_logs(self, params: Dict[str, str], query: Dict[str, List[str]], body: bytes) -> None:
        if self.command == "GET":
            return self._json(200, {"logs": list(self.state.logs)})
        self.state.logs = []
        return self._json(200, {"ok": True})

    def h_error_logs(self, params: Dict[str, str], query: Dict[str, List[str]],
                     body: bytes) -> None:
        return self._json(200, {"files": [{"name": k, "size": len(v)}
                                          for k, v in self.state.error_logs.items()]})

    def h_error_log_file(self, params: Dict[str, str], query: Dict[str, List[str]],
                         body: bytes) -> None:
        name = params.get("name") or ""
        if name not in self.state.error_logs:
            return self._json(404, {"error": "not found"})
        return self._text(200, self.state.error_logs[name])

    def h_plugins(self, params: Dict[str, str], query: Dict[str, List[str]], body: bytes) -> None:
        return self._json(200, {"plugins": []})

    # --- OAuth ---

    def h_oauth_url(self, params: Dict[str, str], query: Dict[str, List[str]],
                    body: bytes) -> None:
        provider = query.get("provider", [""])[0]
        if not provider:
            return self._json(400, {"error": "provider is required"})
        state = uuid.uuid4().hex
        with self.state.lock:
            self.state.oauth_sessions[state] = {"provider": provider, "status": "wait"}
        url = f"https://auth.example.com/{provider}?state={state}"
        return self._json(200, {"url": url, "state": state, "provider": provider})

    def h_oauth_status(self, params: Dict[str, str], query: Dict[str, List[str]],
                       body: bytes) -> None:
        state = query.get("state", [""])[0]
        session = self.state.oauth_sessions.get(state)
        if not session:
            return self._json(404, {"error": "unknown state"})
        return self._json(200, {"status": session["status"], "state": state})

    def h_oauth_session(self, params: Dict[str, str], query: Dict[str, List[str]],
                        body: bytes) -> None:
        state = query.get("state", [""])[0]
        self.state.oauth_sessions.pop(state, None)
        return self._json(200, {"ok": True})


# v0 的 provider 专用 OAuth 端点
def _v0_provider_auth_url(provider: str) -> Callable:
    def handler(self: MockHandlerClass, params: Dict[str, str], query: Dict[str, List[str]],
                body: bytes) -> None:
        state = uuid.uuid4().hex
        with self.state.lock:
            self.state.oauth_sessions[state] = {"provider": provider, "status": "wait"}
        return self._json(200, {"url": f"https://auth.example.com/{provider}?state={state}",
                                "state": state})
    return handler


def _deep_merge(target: Dict[str, Any], patch: Dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def _r(method: str, pattern: str):
    return (method, re.compile("^" + pattern + "$"))


_ROUTES_V8 = [
    _r("GET|PUT|PATCH", r"/config") + (MockHandlerClass.h_config,),
    _r("GET|PUT", r"/config\.yaml") + (MockHandlerClass.h_config_yaml,),
    _r("GET|PUT|DELETE", r"/config/access/api-keys") + (MockHandlerClass.h_config_access_keys,),
    _r("GET|POST|DELETE", r"/credentials") + (MockHandlerClass.h_auth_files,),
    _r("GET", r"/credentials/download") + (MockHandlerClass.h_auth_files_download,),
    _r("GET", r"/credentials/models") + (MockHandlerClass.h_auth_files_models,),
    _r("PATCH", r"/credentials/status") + (MockHandlerClass.h_auth_files_status,),
    _r("PATCH", r"/credentials/fields") + (MockHandlerClass.h_auth_files_fields,),
    _r("POST", r"/credentials/refresh") + (MockHandlerClass.h_auth_files_refresh,),
    _r("GET", r"/observability/usage/queue") + (MockHandlerClass.h_usage_queue,),
    _r("GET", r"/observability/usage/api-keys") + (MockHandlerClass.h_api_key_usage,),
    _r("GET|DELETE", r"/observability/logs") + (MockHandlerClass.h_logs,),
    _r("GET", r"/observability/logs/errors") + (MockHandlerClass.h_error_logs,),
    _r("GET", r"/observability/logs/errors/(?P<name>[^/]+)") + (MockHandlerClass.h_error_log_file,),
    _r("GET|DELETE", r"/observability/logs/requests/(?P<request_id>[^/]+)")
    + (MockHandlerClass.h_error_log_file,),
    _r("GET", r"/server/latest-version") + (MockHandlerClass.h_latest_version,),
    _r("GET", r"/plugins") + (MockHandlerClass.h_plugins,),
    _r("GET", r"/oauth/auth-url") + (MockHandlerClass.h_oauth_url,),
    _r("GET", r"/oauth/status") + (MockHandlerClass.h_oauth_status,),
    _r("DELETE", r"/oauth/session") + (MockHandlerClass.h_oauth_session,),
]

_ROUTES_V0 = [
    _r("GET|PUT|PATCH", r"/config") + (MockHandlerClass.h_config,),
    _r("GET|PUT", r"/config\.yaml") + (MockHandlerClass.h_config_yaml,),
    _r("GET|POST|DELETE", r"/auth-files") + (MockHandlerClass.h_auth_files,),
    _r("GET", r"/auth-files/download") + (MockHandlerClass.h_auth_files_download,),
    _r("GET", r"/auth-files/models") + (MockHandlerClass.h_auth_files_models,),
    _r("PATCH", r"/auth-files/status") + (MockHandlerClass.h_auth_files_status,),
    _r("PATCH", r"/auth-files/fields") + (MockHandlerClass.h_auth_files_fields,),
    _r("POST", r"/auth-files/refresh") + (MockHandlerClass.h_auth_files_refresh,),
    _r("GET", r"/usage-queue") + (MockHandlerClass.h_usage_queue,),
    _r("GET", r"/api-key-usage") + (MockHandlerClass.h_api_key_usage,),
    _r("GET|PUT|PATCH|DELETE", r"/api-keys") + (MockHandlerClass.h_api_keys,),
    _r("GET|PUT", r"/usage-statistics-enabled") + (MockHandlerClass.h_usage_stats_flag,),
    _r("GET", r"/latest-version") + (MockHandlerClass.h_latest_version,),
    _r("GET|DELETE", r"/logs") + (MockHandlerClass.h_logs,),
    _r("GET", r"/request-error-logs") + (MockHandlerClass.h_error_logs,),
    _r("GET", r"/request-error-logs/(?P<name>[^/]+)") + (MockHandlerClass.h_error_log_file,),
    _r("GET", r"/plugins") + (MockHandlerClass.h_plugins,),
    _r("GET", r"/get-auth-status") + (MockHandlerClass.h_oauth_status,),
    _r("DELETE", r"/oauth-session") + (MockHandlerClass.h_oauth_session,),
] + [
    _r("GET", rf"/{provider}-auth-url") + (_v0_provider_auth_url(provider),)
    for provider in ("anthropic", "codex", "antigravity", "kimi", "kimi-ai", "xai", "devin", "meta")
]


if __name__ == "__main__":  # pragma: no cover - 手动起一个 mock 便于调试
    import sys
    prefix = sys.argv[1] if len(sys.argv) > 1 else "v8"
    key = sys.argv[2] if len(sys.argv) > 2 else "sk-mock-key"
    mock = MockCPA(management_key=key, prefix=prefix).start()
    mock.seed_credentials(SAMPLE_CREDENTIALS)
    mock.push_usage(*SAMPLE_USAGE)
    print(f"Mock CPA 运行在 {mock.base_url}（prefix={prefix}, key={key}）")
    print(f"  凭证列表: {mock.base_url}/{prefix}/management/"
          + ("credentials" if prefix == "v8" else "auth-files"))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        mock.stop()
