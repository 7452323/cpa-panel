"""内置 Web 服务：JSON API + 静态单页前端 + Prometheus 指标。

实现选择：标准库 `ThreadingHTTPServer`。理由——
面板要能被丢到任何一台只有 Python 的机器上跑（包括 iOS/VPS），
引入框架会把部署成本抬高，而这里的并发量（一个人用）根本不需要 ASGI。

安全模型：
* 会话 Cookie（HttpOnly, SameSite=Lax），令牌在库里只存 SHA-256；
* 也支持 `Authorization: Bearer <panel-token>` 给脚本/CI 用；
* 所有写操作要求 `X-CPA-Panel: 1` 头（同源 SPA 会带），挡 CSRF；
* 上游管理密钥**永不出现在响应里**（只回 masked）。
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import APP_NAME, __version__
from ..cpa import CPAError, CPAClient, OAUTH_PROVIDERS
from ..log import get
from ..models import credential_view, normalize_usage_record
from ..security import constant_time_eq, hash_password, new_token, token_hash, verify_password
from ..util import (as_bool, as_int, day_of, jdump, jload, mask_secret, now_ts, short)

log = get("cpapanel.web")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
SESSION_COOKIE = "cpa_session"
CSRF_HEADER = "X-CPA-Panel"
MAX_BODY = 8 * 1024 * 1024

ROUTES: List[Tuple[str, "re.Pattern", Callable]] = []


def route(method: str, pattern: str):
    def deco(fn: Callable) -> Callable:
        ROUTES.append((method.upper(), re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, **extra: Any):
        super().__init__(message)
        self.status = status
        self.extra = extra


class Request:
    def __init__(self, method: str, path: str, query: Dict[str, List[str]], headers: Any,
                 body: bytes, client_ip: str):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.client_ip = client_ip
        self.user: Optional[str] = None
        self.role: str = "anonymous"
        self.auth_via: str = ""

    # ---------------------------------------------------------------- 读取

    def q(self, name: str, default: Any = None) -> Any:
        values = self.query.get(name)
        return values[0] if values else default

    def qi(self, name: str, default: Optional[int] = None) -> Optional[int]:
        return as_int(self.q(name), default)

    def qb(self, name: str, default: bool = False) -> bool:
        value = self.q(name)
        return default if value is None else as_bool(value)

    def json(self) -> Dict[str, Any]:
        if not self.body:
            return {}
        data = jload(self.body, None)
        if data is None:
            raise ApiError("请求体不是合法 JSON", 400)
        if not isinstance(data, dict):
            raise ApiError("请求体必须是 JSON 对象", 400)
        return data

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    @property
    def content_type(self) -> str:
        return (self.headers.get("Content-Type") or "").lower()


class PanelApp:
    """把 store / config / collector / inspector / notifier 组装成一个可路由的应用。"""

    def __init__(self, config: Any, store: Any, pricing: Any, collector: Any, inspector: Any,
                 notifier: Any):
        self.config = config
        self.store = store
        self.pricing = pricing
        self.collector = collector
        self.inspector = inspector
        self.notifier = notifier
        self.started_at = now_ts()
        self._oauth_sessions: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 客户端

    def client(self, node_id: Optional[int]) -> CPAClient:
        if not node_id:
            nodes = self.store.list_nodes(only_enabled=True)
            if not nodes:
                raise ApiError("尚未配置任何 CPA 节点", 400, code="no_node")
            node = nodes[0]
        else:
            node = self.store.get_node(int(node_id))
            if not node:
                raise ApiError("节点不存在", 404)
        return CPAClient(
            base_url=node["base_url"], management_key=node["management_key"],
            prefix=node["api_prefix"] if node["api_prefix"] in ("v0", "v8") else "auto",
            timeout=(float(self.config.get("http.connect_timeout") or 5),
                     float(self.config.get("http.read_timeout") or 20)),
            verify_tls=bool(self.config.get("http.verify_tls", True)))

    def node_view(self, row: Any) -> Dict[str, Any]:
        item = dict(row)
        item["management_key"] = mask_secret(item.get("management_key") or "")
        item["has_key"] = bool(row["management_key"])
        item["enabled"] = bool(item.get("enabled"))
        return item

    # ------------------------------------------------------------------ 鉴权

    def authenticate(self, req: Request) -> None:
        header = req.headers.get("Authorization") or ""
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
            record = self.store.find_panel_token(token_hash(token))
            if record:
                self.store.touch_panel_token(int(record["id"]))
                req.user = f"token:{record['name']}"
                req.role = record["role"]
                req.auth_via = "token"
                return
        cookie = _cookie(req.headers.get("Cookie") or "", SESSION_COOKIE)
        if cookie:
            session = self.store.get_session(cookie)
            if session:
                user = self.store.q1("SELECT * FROM users WHERE id = ?", (session["user_id"],))
                if user and not user["disabled"]:
                    req.user = user["username"]
                    req.role = user["role"] or "admin"
                    req.auth_via = "session"
                    return
        req.role = "anonymous"

    def require(self, req: Request, write: bool = False) -> None:
        if req.role == "anonymous":
            raise ApiError("未登录", 401)
        if write and req.role not in ("admin",):
            raise ApiError("需要管理员权限", 403)
        if write and req.auth_via == "session":
            if (req.headers.get(CSRF_HEADER) or "") != "1":
                raise ApiError("缺少 CSRF 头", 403)


class PanelHandler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{__version__}"
    app: PanelApp = None  # 由 serve() 注入

    # ------------------------------------------------------------------ HTTP

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102 - 交给 logging
        log.debug("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:      # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:     # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:      # noqa: N802
        self._handle("PUT")

    def do_PATCH(self) -> None:    # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:   # noqa: N802
        self._handle("DELETE")

    # ------------------------------------------------------------------ 分发

    def _handle(self, method: str) -> None:
        started = time.time()
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
            length = as_int(self.headers.get("Content-Length"), 0) or 0
            if length > MAX_BODY:
                raise ApiError("请求体过大", 413)
            body = self.rfile.read(length) if length else b""
            req = Request(method, path, query, self.headers, body, self.client_ip())
            self.app.authenticate(req)

            if not path.startswith("/api/") and path != "/metrics":
                return self._serve_static(path)

            # 公开端点
            # 注意：/api/session 必须公开 —— 前端靠它判断“未登录→显示登录页”，
            # 若这里要求鉴权，未登录时只能拿到 401，前端就无法区分“未登录”和“服务异常”。
            if path in ("/api/health", "/api/login", "/api/session") or path == "/metrics":
                handler, params = self._match(method, path)
                if handler is None:
                    raise ApiError("未知接口", 404)
                req.path_params = params
                return self._run(handler, req, method, path)

            self.app.require(req, write=method in ("POST", "PUT", "PATCH", "DELETE"))
            handler, params = self._match(method, path)
            if handler is None:
                raise ApiError(f"未知接口 {method} {path}", 404)
            req.path_params = params
            return self._run(handler, req, method, path)
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc), **exc.extra})
        except CPAError as exc:
            self._json(502, exc.to_dict())
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不让线程崩掉
            log.exception("处理 %s %s 失败", method, self.path)
            self._json(500, {"error": str(exc), "type": type(exc).__name__})
        finally:
            log.debug("%s %s %.0fms", method, self.path, (time.time() - started) * 1000)

    def _match(self, method: str, path: str):
        for route_method, pattern, fn in ROUTES:
            match = pattern.match(path)
            if match:
                if route_method != method:
                    continue
                return fn, match.groupdict()
        return None, {}

    def _run(self, handler: Callable, req: Request, method: str, path: str) -> None:
        result = handler(self.app, req)
        if isinstance(result, tuple):
            status, payload = result[0], result[1]
            headers = result[2] if len(result) > 2 else None
        else:
            status, payload, headers = 200, result, None
        if isinstance(payload, (bytes, bytearray)):
            self._bytes(status, bytes(payload), headers)
        else:
            self._json(status, payload, headers)

    # ------------------------------------------------------------------ 响应

    def _json(self, status: int, payload: Any, headers: Optional[Dict[str, str]] = None) -> None:
        body = jdump(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, status: int, body: bytes, headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        ctype = (headers or {}).pop("Content-Type", "application/octet-stream")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/", "/index.html") else path.lstrip("/")
        if ".." in rel or rel.startswith("/"):
            return self._json(400, {"error": "非法路径"})
        target = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not target.startswith(STATIC_DIR):
            return self._json(400, {"error": "非法路径"})
        if not os.path.isfile(target):
            # SPA 回退：未知路径一律返回 index.html
            target = os.path.join(STATIC_DIR, "index.html")
            if not os.path.isfile(target):
                return self._json(404, {"error": "前端资源缺失"})
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or "javascript" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def client_ip(self) -> str:
        if self.app and self.app.config.get("trust_proxy_headers"):
            forwarded = self.headers.get("X-Forwarded-For")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return self.client_address[0] if self.client_address else ""


# =========================================================================== 路由
# 说明：每个处理函数都是 (app, req) -> payload 或 (status, payload, headers)


def _cookie(raw: str, name: str) -> str:
    for part in (raw or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""


# --------------------------------------------------------------------------- 会话


@route("POST", r"/api/login")
def login(app: PanelApp, req: Request) -> Any:
    data = req.json()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    user = app.store.get_user(username)
    if not user or user["disabled"] or not verify_password(password, user["password_hash"]):
        app.store.audit("login.failed", actor=username or "?", ip=req.client_ip)
        raise ApiError("用户名或密码错误", 401)
    ttl = int(app.config.get("admin.session_ttl_seconds") or 604800)
    token = new_token()
    app.store.create_session(token, int(user["id"]), ttl, req.client_ip,
                             short(req.headers.get("User-Agent") or "", 200))
    app.store.touch_login(int(user["id"]))
    app.store.audit("login.success", actor=username, ip=req.client_ip)
    cookie = (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={ttl}"
              + ("; Secure" if _is_https(app, req) else ""))
    return 200, {"ok": True, "username": username, "role": user["role"]}, {"Set-Cookie": cookie}


@route("POST", r"/api/logout")
def logout(app: PanelApp, req: Request) -> Any:
    token = _cookie(req.headers.get("Cookie") or "", SESSION_COOKIE)
    if token:
        app.store.delete_session(token)
    cookie = f"{SESSION_COOKIE}=; Path=/; HttpOnly; Max-Age=0"
    return 200, {"ok": True}, {"Set-Cookie": cookie}


@route("GET", r"/api/session")
def session_info(app: PanelApp, req: Request) -> Any:
    if req.role == "anonymous":
        return {"authenticated": False, "version": __version__, "app": APP_NAME}
    return {"authenticated": True, "username": req.user, "role": req.role,
            "version": __version__, "app": APP_NAME}


@route("POST", r"/api/password")
def change_password(app: PanelApp, req: Request) -> Any:
    data = req.json()
    old = str(data.get("old_password") or "")
    new = str(data.get("new_password") or "")
    if len(new) < 8:
        raise ApiError("新口令至少 8 位", 400)
    if req.auth_via == "token":
        raise ApiError("请用会话登录后再改口令", 400)
    user = app.store.get_user(req.user or "")
    if not user or not verify_password(old, user["password_hash"]):
        raise ApiError("原口令不正确", 403)
    app.store.set_user_password(user["username"], hash_password(new))
    app.store.audit("password.changed", actor=user["username"], ip=req.client_ip)
    return {"ok": True}


@route("GET", r"/api/tokens")
def list_tokens(app: PanelApp, req: Request) -> Any:
    return {"tokens": [dict(r) for r in app.store.list_panel_tokens()]}


@route("POST", r"/api/tokens")
def create_token(app: PanelApp, req: Request) -> Any:
    data = req.json()
    name = str(data.get("name") or "token").strip() or "token"
    role = "viewer" if data.get("role") == "viewer" else "admin"
    raw = new_token()
    app.store.create_panel_token(name, token_hash(raw), role)
    app.store.audit("token.created", actor=req.user or "?", target=name, ip=req.client_ip)
    return {"ok": True, "token": raw, "note": "明文只显示这一次，请立即保存"}


@route("DELETE", r"/api/tokens/(?P<token_id>\d+)")
def revoke_token(app: PanelApp, req: Request) -> Any:
    token_id = int(req.path_params["token_id"])
    app.store.revoke_panel_token(token_id)
    app.store.audit("token.revoked", actor=req.user or "?", target=str(token_id), ip=req.client_ip)
    return {"ok": True}


def _is_https(app: PanelApp, req: Request) -> bool:
    return bool(app.config.get("public_url", "").startswith("https://"))


# --------------------------------------------------------------------------- 节点


@route("GET", r"/api/nodes")
def nodes_list(app: PanelApp, req: Request) -> Any:
    counts = {int(n["id"]): app.store.credential_counts(int(n["id"]))
              for n in app.store.list_nodes()}
    return {"nodes": [dict(app.node_view(n), counts=counts.get(int(n["id"]), {}))
                      for n in app.store.list_nodes()]}


@route("POST", r"/api/nodes")
def node_create(app: PanelApp, req: Request) -> Any:
    data = req.json()
    base_url = str(data.get("base_url") or "").strip().rstrip("/")
    if not base_url.startswith("http"):
        raise ApiError("base_url 必须以 http:// 或 https:// 开头", 400)
    prefix = str(data.get("api_prefix") or "auto")
    if prefix not in ("auto", "v0", "v8"):
        raise ApiError("api_prefix 只能是 auto/v0/v8", 400)
    node_id = app.store.add_node(str(data.get("name") or "default"), base_url,
                                 str(data.get("management_key") or ""), prefix,
                                 enabled=bool(data.get("enabled", True)))
    app.store.audit("node.created", actor=req.user or "?", target=base_url, ip=req.client_ip)
    return {"ok": True, "id": node_id}


@route("PATCH", r"/api/nodes/(?P<node_id>\d+)")
def node_update(app: PanelApp, req: Request) -> Any:
    data = req.json()
    node_id = int(req.path_params["node_id"])
    fields: Dict[str, Any] = {}
    for key in ("name", "base_url", "api_prefix"):
        if key in data:
            fields[key] = data[key]
    if "management_key" in data and str(data["management_key"]).strip():
        fields["management_key"] = str(data["management_key"]).strip()
    if "enabled" in data:
        fields["enabled"] = 1 if as_bool(data["enabled"]) else 0
    app.store.update_node(node_id, **fields)
    app.store.audit("node.updated", actor=req.user or "?", target=str(node_id),
                    detail=jdump({k: ("***" if k == "management_key" else v)
                                  for k, v in fields.items()}), ip=req.client_ip)
    return {"ok": True}


@route("DELETE", r"/api/nodes/(?P<node_id>\d+)")
def node_delete(app: PanelApp, req: Request) -> Any:
    node_id = int(req.path_params["node_id"])
    app.store.delete_node(node_id)
    app.store.audit("node.deleted", actor=req.user or "?", target=str(node_id), ip=req.client_ip)
    return {"ok": True}


@route("POST", r"/api/nodes/(?P<node_id>\d+)/test")
def node_test(app: PanelApp, req: Request) -> Any:
    node_id = int(req.path_params["node_id"])
    node = app.store.get_node(node_id)
    if not node:
        raise ApiError("节点不存在", 404)
    client = app.client(node_id)
    result = client.probe()
    if result.get("ok"):
        app.store.update_node(node_id, last_ok_at=now_ts(), last_error="",
                              detected_prefix=result.get("prefix"), version=result.get("version"))
    else:
        app.store.update_node(node_id, last_error=short(result.get("error"), 400))
    return result


# --------------------------------------------------------------------------- 凭证


@route("GET", r"/api/credentials")
def credentials_list(app: PanelApp, req: Request) -> Any:
    standby = None
    if req.q("standby") is not None:
        standby = as_bool(req.q("standby"))
    rows = app.store.list_credentials(
        node_id=req.qi("node_id"), provider=req.q("provider", "") or "",
        status=req.q("status", "") or "", keyword=req.q("q", "") or "",
        present_only=not req.qb("include_removed"),
        standby=standby)
    items = [credential_view(r) for r in rows]
    state = req.q("state")
    if state:
        items = [i for i in items if i.get("state") == state]
    return {"credentials": items, "total": len(items),
            "counts": app.store.credential_counts(req.qi("node_id")),
            "providers": app.store.provider_breakdown(req.qi("node_id"))}


@route("POST", r"/api/credentials/sync")
def credentials_sync(app: PanelApp, req: Request) -> Any:
    data = req.json()
    node_id = as_int(data.get("node_id")) or (app.store.list_nodes(only_enabled=True)[0]["id"]
                                              if app.store.list_nodes(only_enabled=True) else None)
    if not node_id:
        raise ApiError("尚未配置任何 CPA 节点", 400)
    client = app.client(node_id)
    raw = client.list_auth_files()
    entries = [e for e in (raw.get("files") or []) if isinstance(e, dict)]
    from ..models import normalize_auth_file
    normalized = [normalize_auth_file(e) for e in entries]
    result = app.store.sync_credentials(int(node_id), normalized)
    app.store.audit("credentials.synced", actor=req.user or "?", target=str(node_id),
                    detail=jdump({k: v for k, v in result.items() if k != "changes"}),
                    ip=req.client_ip)
    return {"ok": True, "observed_at": raw.get("observed_at"), **result}


@route("GET", r"/api/credentials/(?P<cred_id>\d+)")
def credential_detail(app: PanelApp, req: Request) -> Any:
    row = app.store.get_credential(int(req.path_params["cred_id"]))
    if not row:
        raise ApiError("凭证不存在", 404)
    view = credential_view(row)
    view["samples"] = app.store.credential_samples(int(row["id"]), limit=120)
    return {"credential": view}


@route("PATCH", r"/api/credentials/(?P<cred_id>\d+)")
def credential_update(app: PanelApp, req: Request) -> Any:
    """修改凭证：本地元数据 + 同步推给上游（disabled / priority / note）。"""
    cred_id = int(req.path_params["cred_id"])
    row = app.store.get_credential(cred_id)
    if not row:
        raise ApiError("凭证不存在", 404)
    data = req.json()
    client = app.client(int(row["node_id"]))
    pushed: Dict[str, Any] = {}
    if "disabled" in data:
        disabled = as_bool(data["disabled"])
        client.patch_auth_file_status(row["name"], disabled)
        app.store.ex("UPDATE credentials SET disabled = ? WHERE id = ?",
                     (1 if disabled else 0, cred_id))
        pushed["disabled"] = disabled
    fields = {k: data[k] for k in ("priority", "note", "weight", "websockets", "label")
              if k in data}
    if fields:
        client.patch_auth_file_fields(row["name"], fields)
        sets, args = [], []
        for key, value in fields.items():
            if key in ("priority", "note", "weight", "websockets"):
                sets.append(f"{key} = ?")
                args.append(value)
        if sets:
            args.append(cred_id)
            app.store.ex(f"UPDATE credentials SET {', '.join(sets)} WHERE id = ?", args)
        pushed.update(fields)
    if "standby" in data:
        app.store.set_credential_standby(cred_id, as_bool(data["standby"]))
        pushed["standby"] = as_bool(data["standby"])
    app.store.audit("credential.updated", actor=req.user or "?", target=row["name"],
                    detail=jdump(pushed), ip=req.client_ip)
    return {"ok": True, "pushed": pushed}


CREDENTIAL_ACTIONS = ("refresh", "disable", "enable", "delete", "standby", "promote",
                     "reset_cooldown")


def _run_credential_action(app: PanelApp, row: Any, action: str) -> Dict[str, Any]:
    """对单个凭证执行一个动作。

    单发（`/action`）与批量（`/batch`）共用这一份实现 —— 否则两条路的语义迟会漂，
    而“单点能用、批量偷偷不一样”是这类面板最难查的 bug。
    """
    cred_id = int(row["id"])
    node_id, name = int(row["node_id"]), row["name"]
    if action == "refresh":
        return app.inspector.refresh_credential(node_id, name)
    if action == "disable":
        return app.inspector.disable_credential(node_id, name)
    if action == "enable":
        return app.inspector.enable_credential(node_id, name)
    if action == "delete":
        return app.inspector.delete_credential(node_id, name)
    if action == "standby":
        result = app.inspector._manual(node_id, name, "standby")
        if result.get("ok"):
            app.store.set_credential_standby(cred_id, True)
        return result
    if action == "promote":
        result = app.inspector._manual(node_id, name, "promote")
        if result.get("ok"):
            app.store.set_credential_standby(cred_id, False)
        return result
    if action == "reset_cooldown":
        # 上游只认 auth_index（不是 name）；本地缺 auth_index 时会让用户看到原因
        return app.inspector.reset_cooldown_credential(node_id, name,
                                                       str(row["auth_index"] or ""))
    raise ApiError(f"未知动作：{action}", 400)


@route("POST", r"/api/credentials/(?P<cred_id>\d+)/action")
def credential_action(app: PanelApp, req: Request) -> Any:
    cred_id = int(req.path_params["cred_id"])
    row = app.store.get_credential(cred_id)
    if not row:
        raise ApiError("凭证不存在", 404)
    action = str(req.json().get("action") or "").strip().lower()
    result = _run_credential_action(app, row, action)
    app.store.audit(f"credential.{action}", actor=req.user or "?", target=row["name"],
                    detail=short(jdump(result), 300), ip=req.client_ip)
    return result


@route("POST", r"/api/credentials/batch")
def credentials_batch(app: PanelApp, req: Request) -> Any:
    """批量操作（面板上勾一批号一次处理）。

    两个刻意的设计：
      1. `delete` 受 `inspector.max_deletes_per_run` 限制 —— 巡检的单轮删除上限
         不能因为“手点一下”就绕过，否则那个闸门形同虚设；
      2. 批量**不含**清空全部，那个必须走 `/delete-all` 并输入确认词。
    每项都单独返回结果：一个号失败不应该把整批标成失败，也不应该让剩下的悄悄不执行。
    """
    data = req.json()
    action = str(data.get("action") or "").strip().lower()
    ids = data.get("ids") or []
    if action not in CREDENTIAL_ACTIONS:
        raise ApiError(f"未知动作：{action}；可选：{', '.join(CREDENTIAL_ACTIONS)}", 400)
    if not isinstance(ids, list) or not ids:
        raise ApiError("ids 必须是非空数组", 400)
    max_items = 200
    if len(ids) > max_items:
        raise ApiError(f"一次最多处理 {max_items} 个", 400)
    if action == "delete":
        limit = int(app.config.get("inspector.max_deletes_per_run") or 20)
        if len(ids) > limit:
            raise ApiError(
                f"单次最多删除 {limit} 个（与巡检的 inspector.max_deletes_per_run 一致）", 400)

    results: List[Dict[str, Any]] = []
    for raw_id in ids:
        try:
            cred_id = int(raw_id)
        except (TypeError, ValueError):
            results.append({"id": raw_id, "ok": False, "error": "id 不是数字"})
            continue
        row = app.store.get_credential(cred_id)
        if not row:
            results.append({"id": cred_id, "ok": False, "error": "凭证不存在"})
            continue
        try:
            outcome = _run_credential_action(app, row, action)
        except ApiError as exc:
            outcome = {"ok": False, "error": str(exc)}
        results.append({"id": cred_id, "name": row["name"], **outcome})

    succeeded = sum(1 for item in results if item.get("ok"))
    app.store.audit(f"credential.batch.{action}", actor=req.user or "?",
                    target=f"{succeeded}/{len(results)}", ip=req.client_ip)
    return {"ok": succeeded == len(results), "action": action,
            "requested": len(ids), "succeeded": succeeded, "results": results}


@route("POST", r"/api/credentials/delete-all")
def credentials_delete_all(app: PanelApp, req: Request) -> Any:
    """清空上游全部凭证（官方：`DELETE /credentials?all=true`）。

    这是本项目**唯一一个不可逆且没有数量上限**的动作，所以：
      * 必须带上确认词 `DELETE-ALL`（防误触、防前端按钮点错）；
      * 不受单轮删除上限保护 —— 因此只能由人明确触发，巡检永远不会调它；
      * 强制写审计，记下是谁在什么时候清的。
    """
    try:
        data = req.json()
    except Exception:
        data = {}
    if not isinstance(data, dict) or str(data.get("confirm") or "").strip() != "DELETE-ALL":
        raise ApiError("危险操作：请在 confirm 字段里原样输入 DELETE-ALL", 400)
    node_id = as_int(data.get("node_id")) or (app.store.list_nodes(only_enabled=True)[0]["id"]
                                              if app.store.list_nodes(only_enabled=True) else None)
    if not node_id:
        raise ApiError("尚未配置任何 CPA 节点", 400)
    client = app.client(node_id)
    result = client.delete_all_auth_files()
    app.store.audit("credential.delete_all", actor=req.user or "?", target=f"node={node_id}",
                    detail=short(jdump(result), 300), ip=req.client_ip)
    return {"ok": True, "node_id": node_id, "result": result}


@route("GET", r"/api/credentials/(?P<cred_id>\d+)/models")
def credential_models(app: PanelApp, req: Request) -> Any:
    row = app.store.get_credential(int(req.path_params["cred_id"]))
    if not row:
        raise ApiError("凭证不存在", 404)
    return {"models": app.client(int(row["node_id"])).auth_file_models(row["name"])}


@route("GET", r"/api/credentials/(?P<cred_id>\d+)/download")
def credential_download(app: PanelApp, req: Request) -> Any:
    row = app.store.get_credential(int(req.path_params["cred_id"]))
    if not row:
        raise ApiError("凭证不存在", 404)
    body = app.client(int(row["node_id"])).download_auth_file(row["name"])
    filename = row["name"] if row["name"].endswith(".json") else f"{row['name']}.json"
    app.store.audit("credential.downloaded", actor=req.user or "?", target=row["name"],
                    ip=req.client_ip)
    return 200, body, {"Content-Type": "application/json",
                       "Content-Disposition": f'attachment; filename="{filename}"'}


@route("POST", r"/api/credentials/import")
def credential_import(app: PanelApp, req: Request) -> Any:
    """multipart 上传凭证文件（字段名 file）。"""
    files, fields = _parse_multipart(req)
    if not files:
        raise ApiError("未找到上传文件（字段名应为 file）", 400)
    node_id = as_int(fields.get("node_id")) or (app.store.list_nodes(only_enabled=True)[0]["id"]
                                                if app.store.list_nodes(only_enabled=True) else None)
    if not node_id:
        raise ApiError("尚未配置任何 CPA 节点", 400)
    client = app.client(int(node_id))
    results = []
    for filename, content in files:
        if not filename.lower().endswith(".json"):
            results.append({"filename": filename, "ok": False, "error": "上游只接受 .json"})
            continue
        try:
            payload = jload(content, None)
            if payload is None:
                results.append({"filename": filename, "ok": False, "error": "不是合法 JSON"})
                continue
        except Exception:
            results.append({"filename": filename, "ok": False, "error": "解析失败"})
            continue
        try:
            client.upload_auth_file(filename, content)
            results.append({"filename": filename, "ok": True})
        except CPAError as exc:
            results.append({"filename": filename, "ok": False, "error": str(exc)})
    app.store.audit("credential.imported", actor=req.user or "?", target=str(node_id),
                    detail=jdump([r.get("filename") for r in results if r.get("ok")]),
                    ip=req.client_ip)
    return {"ok": all(r["ok"] for r in results) if results else False, "results": results}


@route("GET", r"/api/credentials/events")
def credential_events(app: PanelApp, req: Request) -> Any:
    limit = req.qi("limit", 100) or 100
    node_id = req.qi("node_id")
    sql = "SELECT * FROM credential_events WHERE 1=1"
    args: List[Any] = []
    if node_id:
        sql += " AND node_id = ?"
        args.append(node_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return {"events": [dict(r) for r in app.store.q(sql, args)]}


# --------------------------------------------------------------------------- OAuth


@route("POST", r"/api/oauth/start")
def oauth_start(app: PanelApp, req: Request) -> Any:
    data = req.json()
    provider = str(data.get("provider") or "").strip().lower()
    if provider not in OAUTH_PROVIDERS:
        raise ApiError(f"provider 必须是 {', '.join(OAUTH_PROVIDERS)} 之一", 400)
    node_id = as_int(data.get("node_id"))
    client = app.client(node_id)
    result = client.start_oauth(provider)
    state = result.get("state") or new_token(12)
    with app._lock:
        app._oauth_sessions[state] = {"provider": provider, "node_id": node_id,
                                      "created_at": now_ts(), "url": result.get("url")}
    app.store.audit("oauth.started", actor=req.user or "?", target=provider, ip=req.client_ip)
    return {"ok": True, **result, "state": state}


@route("GET", r"/api/oauth/status")
def oauth_status(app: PanelApp, req: Request) -> Any:
    state = req.q("state") or ""
    if not state:
        raise ApiError("缺少 state", 400)
    with app._lock:
        session = app._oauth_sessions.get(state) or {}
    client = app.client(session.get("node_id"))
    try:
        result = client.oauth_status(state, session.get("provider") or "")
    except CPAError as exc:
        return {"state": state, "status": "error", "error": str(exc)}
    status = str(result.get("status") or result.get("state") or "wait")
    if status.lower() in ("ok", "success", "done"):
        with app._lock:
            app._oauth_sessions.pop(state, None)
    return {"state": state, "status": status, "raw": result}


@route("DELETE", r"/api/oauth/session")
def oauth_cancel(app: PanelApp, req: Request) -> Any:
    state = req.q("state") or ""
    with app._lock:
        session = app._oauth_sessions.pop(state, {})
    if session:
        app.client(session.get("node_id")).cancel_oauth(state)
    return {"ok": True}


# --------------------------------------------------------------------------- 配额


@route("GET", r"/api/quotas")
def quotas(app: PanelApp, req: Request) -> Any:
    client = app.client(req.qi("node_id"))
    providers: Any = []
    try:
        providers = client.quota_providers()
    except CPAError as exc:
        providers = {"error": str(exc), "status": exc.status}
    rows = app.store.list_credentials(node_id=req.qi("node_id"), present_only=True)
    items = []
    for row in rows:
        view = credential_view(row)
        raw_entry = jload(row["raw_json"], {}) or {}
        items.append({
            "id": view.get("id"), "name": view.get("name"), "provider": view.get("provider"),
            "email": view.get("email"), "state": view.get("state"),
            # 这一列来自上游自己的声明，而不是“有没有 raw_json”
            "supports_quota": bool(raw_entry.get("supports_quota")),
            "quota_provider": raw_entry.get("quota_provider") or "",
            "quota": view.get("quota"), "next_retry_after": view.get("next_retry_after"),
            "subscription_until": view.get("subscription_until"),
        })
    return {"providers": providers, "credentials": items,
            "note": "配额来自上游被动观测（quota.signals）；主动查询需上游支持 quota/fetch"}


@route("POST", r"/api/quotas/fetch")
def quota_fetch(app: PanelApp, req: Request) -> Any:
    data = req.json()
    client = app.client(as_int(data.get("node_id")))
    result = client.fetch_quota(name=str(data.get("name") or ""),
                                auth_index=str(data.get("auth_index") or ""))
    app.store.audit("quota.fetched", actor=req.user or "?", target=str(data.get("name") or ""),
                    ip=req.client_ip)
    return {"ok": True, "result": result}


# --------------------------------------------------------------------------- 用量


@route("GET", r"/api/usage/summary")
def usage_summary(app: PanelApp, req: Request) -> Any:
    days = req.qi("days", 7) or 7
    node_id = req.qi("node_id")
    return {
        "summary": app.store.usage_summary(days, node_id),
        "today": app.store.usage_summary(1, node_id),
        "totals": app.store.usage_totals_by_day(node_id),
        "collector": app.collector.snapshot(),
    }


@route("GET", r"/api/usage/series")
def usage_series(app: PanelApp, req: Request) -> Any:
    days = req.qi("days", 14) or 14
    return {"series": app.store.usage_series(days, req.qi("node_id"))}


@route("GET", r"/api/usage/models")
def usage_models(app: PanelApp, req: Request) -> Any:
    days = req.qi("days", 7) or 7
    return {"models": app.store.usage_by_model(days, req.qi("limit", 30) or 30, req.qi("node_id"))}


@route("GET", r"/api/usage/credentials")
def usage_credentials(app: PanelApp, req: Request) -> Any:
    days = req.qi("days", 7) or 7
    return {"credentials": app.store.usage_by_credential(days, req.qi("limit", 50) or 50,
                                                         req.qi("node_id"))}


@route("GET", r"/api/usage/keys")
def usage_keys(app: PanelApp, req: Request) -> Any:
    days = req.qi("days", 7) or 7
    return {"keys": app.store.usage_by_key(days, req.qi("node_id"))}


@route("GET", r"/api/usage/events")
def usage_events(app: PanelApp, req: Request) -> Any:
    since = req.qi("since")
    if req.qi("hours"):
        since = now_ts() - (req.qi("hours") or 0) * 3600
    events = app.store.list_usage_events(
        node_id=req.qi("node_id"), model=req.q("model", "") or "",
        credential=req.q("credential", "") or "", api_key=req.q("api_key", "") or "",
        errors_only=req.qb("errors_only"), since=since,
        limit=req.qi("limit", 100) or 100, offset=req.qi("offset", 0) or 0)
    # 列表接口只回掩码；要看明文 Key / 原始 JSON 请用详情接口
    for item in events:
        item.pop("api_key", None)
        item.pop("raw_json", None)
    return {"events": events}


@route("GET", r"/api/usage/events/(?P<event_id>\d+)")
def usage_event_detail(app: PanelApp, req: Request) -> Any:
    item = app.store.usage_event(int(req.path_params["event_id"]))
    if not item:
        raise ApiError("记录不存在", 404)
    return {"event": item, "raw": jload(item.get("raw_json"), {})}


@route("POST", r"/api/usage/collect")
def usage_collect(app: PanelApp, req: Request) -> Any:
    """手动触发一次采集：既用于排障，也用于「刚配好就想看数据」的场景。

    返回结构**恒为** `{ok, result:{nodes:[...]}}`（即使只采了一个节点），
    否则前端要在两种形状之间分支，很容易出 bug（实测踩过）。
    """
    data = req.json() if req.body else {}
    result = app.collector.collect_once(as_int(data.get("node_id")))
    if isinstance(result, dict) and "nodes" not in result:
        result = {"nodes": [result], "ts": now_ts()}
    app.store.audit("usage.manual_collect", actor=req.user or "?", detail=short(jdump(result), 300),
                    ip=req.client_ip)
    return {"ok": True, "result": result}


# --------------------------------------------------------------------------- 下游 Key


@route("GET", r"/api/keys")
def keys_list(app: PanelApp, req: Request) -> Any:
    return {"keys": app.store.list_api_keys(req.qi("node_id"))}


@route("POST", r"/api/keys/sync")
def keys_sync(app: PanelApp, req: Request) -> Any:
    data = req.json() if req.body else {}
    node_id = as_int(data.get("node_id"))
    client = app.client(node_id)
    keys = client.get_api_keys()
    result = app.store.sync_api_keys(int(node_id or 0), keys)
    return {"ok": True, "keys": keys, **result}


@route("POST", r"/api/keys")
def key_add(app: PanelApp, req: Request) -> Any:
    """下游 Key 是「整表替换」语义（PUT），所以这里做读-改-写。"""
    data = req.json()
    node_id = as_int(data.get("node_id"))
    key = str(data.get("key") or "").strip()
    if not key:
        raise ApiError("key 不能为空", 400)
    client = app.client(node_id)
    keys = client.get_api_keys()
    if key in keys:
        return {"ok": True, "already_exists": True}
    keys.append(key)
    client.put_api_keys(keys)
    app.store.sync_api_keys(int(node_id or 0), keys)
    app.store.audit("key.added", actor=req.user or "?", target=mask_secret(key), ip=req.client_ip)
    return {"ok": True, "count": len(keys)}


@route("DELETE", r"/api/keys")
def key_remove(app: PanelApp, req: Request) -> Any:
    data = req.json() if req.body else {}
    node_id = as_int(data.get("node_id"))
    key = str(data.get("key") or "").strip()
    if not key:
        raise ApiError("key 不能为空", 400)
    client = app.client(node_id)
    keys = [k for k in client.get_api_keys() if k != key]
    client.put_api_keys(keys)
    app.store.sync_api_keys(int(node_id or 0), keys)
    app.store.audit("key.removed", actor=req.user or "?", target=mask_secret(key), ip=req.client_ip)
    return {"ok": True, "count": len(keys)}


# --------------------------------------------------------------------------- 配置


@route("GET", r"/api/config")
def panel_config(app: PanelApp, req: Request) -> Any:
    return {"config": app.config.public_dict(redact=True),
            "path": app.config.path,
            "pricing_models": len(app.pricing.known_models())}


@route("PUT", r"/api/config")
def panel_config_update(app: PanelApp, req: Request) -> Any:
    """更新面板自身的配置（白名单路径，避免被写入任意字段）。"""
    data = req.json()
    allowed = {
        "log_level", "public_url", "trust_proxy_headers", "pricing_file",
        "collector.enabled", "collector.interval_seconds", "collector.batch_size",
        "collector.queue_retention_seconds", "collector.gap_warn_ratio",
        "inspector.enabled", "inspector.interval_seconds", "inspector.dry_run",
        "inspector.disable_unauthorized", "inspector.disable_quota_exhausted",
        "inspector.delete_unauthorized", "inspector.delete_quota_exhausted",
        "inspector.max_deletes_per_run", "inspector.standby_pool", "inspector.target_active",
        "inspector.promote_standby_when_low",
        # 安全闸门：必须可调，否则 UI 里改了也会被这里静默忽略
        "inspector.circuit_breaker_enabled", "inspector.min_ready_ratio",
        "inspector.act_on_weak_evidence",
        "notify.webhook_url", "notify.telegram_bot_token", "notify.telegram_chat_id",
        "notify.min_interval_seconds",
    }
    applied: Dict[str, Any] = {}
    for key, value in data.items():
        if key not in allowed:
            continue
        if key.startswith("inspector.") or key.startswith("collector."):
            # 布尔类的后缀：注意 circuit_breaker_enabled / act_on_weak_evidence
            # 不以 ".enabled" 结尾，必须单独列出来 —— 否则传字符串 "false" 会被当成真值。
            if key.endswith((".enabled", ".dry_run", ".disable_unauthorized",
                             ".disable_quota_exhausted", ".delete_unauthorized",
                             ".delete_quota_exhausted", ".standby_pool",
                             ".promote_standby_when_low",
                             "circuit_breaker_enabled", "act_on_weak_evidence")):
                value = as_bool(value)
            elif key.endswith(("seconds", "batch_size", "max_deletes_per_run", "target_active")):
                value = as_int(value) or 0
            elif key.endswith("ratio"):
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
        app.config.set(key, value)
        applied[key] = value
    app.config.save()
    # 热更新后台线程节奏
    if any(k.startswith("collector.") for k in applied):
        app.collector.ensure_running()
    if any(k.startswith("inspector.") for k in applied):
        app.inspector.ensure_running()
    app.store.audit("config.updated", actor=req.user or "?", detail=jdump(applied),
                    ip=req.client_ip)
    return {"ok": True, "applied": applied}


@route("GET", r"/api/upstream/config")
def upstream_config(app: PanelApp, req: Request) -> Any:
    return {"config": app.client(req.qi("node_id")).get_config()}


@route("GET", r"/api/upstream/config-yaml")
def upstream_config_yaml(app: PanelApp, req: Request) -> Any:
    text = app.client(req.qi("node_id")).get_config_yaml()
    return {"yaml": text}


@route("PUT", r"/api/upstream/config-yaml")
def upstream_config_yaml_put(app: PanelApp, req: Request) -> Any:
    """直接替换上游 config.yaml（PUT 全量替换语义，与上游一致）。"""
    data = req.json()
    text = str(data.get("yaml") or "")
    if not text.strip():
        raise ApiError("yaml 内容为空", 400)
    app.client(as_int(data.get("node_id"))).put_config_yaml(text)
    app.store.audit("upstream.config_replaced", actor=req.user or "?", ip=req.client_ip,
                    detail=f"{len(text)} bytes")
    return {"ok": True, "bytes": len(text)}


# --- OAuth 模型排除 / 模型别名
# 上游只提供“整张 map 一起 PUT”，没有单条增删接口 —— 所以面板做读-改-写，
# UI 只需表达“某个渠道排除哪些模型”。
# 不这么做的话，改一个渠道会把其它渠道的配置静默清空。


@route("GET", r"/api/upstream/oauth-excluded-models")
def upstream_excluded_models(app: PanelApp, req: Request) -> Any:
    return {"providers": app.client(req.qi("node_id")).get_oauth_excluded_models()}


@route("POST", r"/api/upstream/oauth-excluded-models")
def upstream_excluded_models_set(app: PanelApp, req: Request) -> Any:
    """设置某个渠道「不接的模型」；传空数组 = 删除该渠道的条目。"""
    data = req.json()
    provider = str(data.get("provider") or "").strip()
    if not provider:
        raise ApiError("provider 必填", 400)
    models = data.get("models")
    if not isinstance(models, list):
        raise ApiError("models 必须是数组（空数组表示删除该渠道）", 400)
    client = app.client(as_int(data.get("node_id")))
    mapping = client.get_oauth_excluded_models()
    before = mapping.get(provider)
    cleaned = [str(m).strip() for m in models if str(m).strip()]
    if cleaned:
        mapping[provider] = cleaned
    else:
        mapping.pop(provider, None)
    client.put_oauth_excluded_models(mapping)
    app.store.audit("upstream.oauth_excluded_models", actor=req.user or "?", target=provider,
                    detail=short(jdump({"before": before, "after": cleaned}), 300),
                    ip=req.client_ip)
    return {"ok": True, "provider": provider, "models": cleaned, "providers": mapping}


@route("GET", r"/api/upstream/oauth-model-alias")
def upstream_model_alias(app: PanelApp, req: Request) -> Any:
    return {"channels": app.client(req.qi("node_id")).get_oauth_model_alias()}


@route("POST", r"/api/upstream/oauth-model-alias")
def upstream_model_alias_set(app: PanelApp, req: Request) -> Any:
    """设置某个渠道的模型别名；传空数组 = 删除该渠道。"""
    data = req.json()
    channel = str(data.get("channel") or data.get("provider") or "").strip()
    if not channel:
        raise ApiError("channel 必填", 400)
    aliases = data.get("aliases")
    if not isinstance(aliases, list):
        raise ApiError("aliases 必须是数组（空数组表示删除该渠道）", 400)
    normalized = [a for a in aliases
                  if isinstance(a, dict) and a.get("name") and a.get("alias")]
    client = app.client(as_int(data.get("node_id")))
    mapping = client.get_oauth_model_alias()
    before = mapping.get(channel)
    if normalized:
        mapping[channel] = normalized
    else:
        mapping.pop(channel, None)
    client.put_oauth_model_alias(mapping)
    app.store.audit("upstream.oauth_model_alias", actor=req.user or "?", target=channel,
                    detail=short(jdump({"before": before, "after": normalized}), 300),
                    ip=req.client_ip)
    return {"ok": True, "channel": channel, "aliases": normalized, "channels": mapping}


@route("GET", r"/api/upstream/request-log")
def upstream_request_log(app: PanelApp, req: Request) -> Any:
    """请求日志开关的当前值。

    从上游 `/config` 里读；读不到就返回 `None` —— 不编一个假值出来。
    """
    config = app.client(req.qi("node_id")).get_config()
    value = None
    observability = config.get("observability") if isinstance(config, dict) else None
    if isinstance(observability, dict):
        for key in ("request-log", "request-log-enabled", "request_log"):
            if key in observability:
                value = observability[key]
                break
    return {"enabled": value}


@route("POST", r"/api/upstream/request-log")
def upstream_request_log_set(app: PanelApp, req: Request) -> Any:
    """开/关上游请求日志。"""
    data = req.json()
    if "enabled" not in data:
        raise ApiError("enabled 必填（true/false）", 400)
    enabled = as_bool(data.get("enabled"))
    app.client(as_int(data.get("node_id"))).set_request_log(enabled)
    app.store.audit("upstream.request_log", actor=req.user or "?", target=str(enabled),
                    ip=req.client_ip)
    return {"ok": True, "enabled": enabled}


# --------------------------------------------------------------------------- 日志


@route("GET", r"/api/logs")
def upstream_logs(app: PanelApp, req: Request) -> Any:
    data = app.client(req.qi("node_id")).get_logs()
    if isinstance(data, dict) and "logs" in data:
        logs = data["logs"]
    else:
        logs = data
    if isinstance(logs, str):
        lines = logs.splitlines()
    elif isinstance(logs, list):
        lines = [str(x) for x in logs]
    else:
        lines = [jdump(logs)]
    limit = req.qi("limit", 500) or 500
    return {"lines": lines[-limit:], "total": len(lines)}


@route("DELETE", r"/api/logs")
def upstream_logs_clear(app: PanelApp, req: Request) -> Any:
    app.client(req.qi("node_id")).delete_logs()
    app.store.audit("upstream.logs_cleared", actor=req.user or "?", ip=req.client_ip)
    return {"ok": True}


@route("GET", r"/api/logs/panel")
def panel_logs(app: PanelApp, req: Request) -> Any:
    from ..log import buffer
    return {"lines": buffer(limit=req.qi("limit", 300) or 300,
                            after_seq=req.qi("after_seq", 0) or 0,
                            level=req.q("level"))}


@route("GET", r"/api/logs/errors")
def upstream_error_logs(app: PanelApp, req: Request) -> Any:
    return {"files": app.client(req.qi("node_id")).error_logs()}


@route("GET", r"/api/logs/errors/(?P<name>[^/]+)")
def upstream_error_log(app: PanelApp, req: Request) -> Any:
    name = req.path_params["name"]
    body = app.client(req.qi("node_id")).download_error_log(name)
    return 200, body, {"Content-Type": "text/plain; charset=utf-8"}


# --------------------------------------------------------------------------- 巡检


@route("GET", r"/api/inspections")
def inspections(app: PanelApp, req: Request) -> Any:
    return {"inspections": app.store.list_inspections(req.qi("node_id"), req.qi("limit", 50) or 50),
            "last": app.inspector.snapshot()}


@route("POST", r"/api/inspections/run")
def inspection_run(app: PanelApp, req: Request) -> Any:
    data = req.json() if req.body else {}
    dry_run = data.get("dry_run")
    result = app.inspector.run(node_id=as_int(data.get("node_id")),
                               dry_run=None if dry_run is None else as_bool(dry_run),
                               reason="manual")
    app.store.audit("inspection.run", actor=req.user or "?",
                    detail=short(jdump(result.get("nodes", [{}])[0] if result.get("nodes") else {}), 300),
                    ip=req.client_ip)
    return {"ok": True, "result": result}


@route("GET", r"/api/actions")
def actions_list(app: PanelApp, req: Request) -> Any:
    return {"actions": app.store.list_actions(req.qi("node_id"), req.qi("limit", 100) or 100,
                                              req.qi("inspection_id"))}


# --------------------------------------------------------------------------- 系统


@route("GET", r"/api/audit")
def audit_list(app: PanelApp, req: Request) -> Any:
    return {"audit": app.store.list_audit(req.qi("limit", 200) or 200, req.q("action", "") or "")}


@route("GET", r"/api/overview")
def overview(app: PanelApp, req: Request) -> Any:
    node_id = req.qi("node_id")
    counts = app.store.credential_counts(node_id)
    days = 7
    return {
        "panel": {"version": __version__, "uptime_seconds": now_ts() - app.started_at,
                  "database": app.store.stats()},
        "nodes": [dict(app.node_view(n)) for n in app.store.list_nodes()],
        "credentials": {"counts": counts,
                        "providers": app.store.provider_breakdown(node_id)},
        "usage": {"summary": app.store.usage_summary(days, node_id),
                  "today": app.store.usage_summary(1, node_id),
                  "series": app.store.usage_series(14, node_id),
                  "top_models": app.store.usage_by_model(days, 8, node_id),
                  "top_credentials": app.store.usage_by_credential(days, 8, node_id),
                  "top_keys": app.store.usage_by_key(days, node_id)[:8]},
        "collector": app.collector.snapshot(),
        "inspector": {"last": app.inspector.snapshot(),
                      "recent": app.store.list_inspections(node_id, 5),
                      "actions": app.store.list_actions(node_id, 10)},
        "alerts": _alerts(app, counts, node_id),
    }


def _alerts(app: PanelApp, counts: Dict[str, int], node_id: Optional[int]) -> List[Dict[str, Any]]:
    """把「值得马上看一眼」的事情显式列出来。"""
    alerts: List[Dict[str, Any]] = []
    if not app.store.list_nodes():
        alerts.append({"level": "warning", "text": "还没有配置 CPA 节点", "action": "在「设置」里添加节点"})
        return alerts
    if counts.get("erroring", 0) > 0:
        alerts.append({"level": "warning",
                       "text": f"{counts['erroring']} 个凭证处于 error 状态",
                       "action": "打开「账号」页筛选 state=unauthorized 处理后重登"})
    if counts.get("cooling", 0) > 0:
        alerts.append({"level": "info", "text": f"{counts['cooling']} 个凭证正在冷却"})
    target = int(app.config.get("inspector.target_active") or 0)
    if target and counts.get("active", 0) < target:
        alerts.append({"level": "error",
                       "text": f"可用账号 {counts.get('active', 0)} 低于目标 {target}",
                       "action": "补充账号或从备用池恢复"})
    for node_key, state in (app.collector.snapshot() or {}).items():
        # 指定了 node_id 时就只看那个节点，否则多节点场景会重复报同一件事
        if node_id and str(node_id) != str(node_key):
            continue
        node_name = node_key
        node_row = app.store.get_node(as_int(node_key) or 0)
        if node_row:
            node_name = node_row["name"]
        last = state.get("last_success_ts")
        retention = int(app.config.get("collector.queue_retention_seconds") or 60)
        if last and now_ts() - int(last) > retention * 3:
            alerts.append({"level": "error",
                           "text": f"节点 {node_name} 已 {now_ts() - int(last)}s 未成功采集",
                           "action": "上游可能不可用；这期间用量记录已被丢弃"})
    if int(app.config.get("inspector.dry_run", True)):
        alerts.append({"level": "info", "text": "巡检处于 dry-run 模式（只计划不执行）",
                       "action": "确认逻辑无误后在「设置 → 巡检」里关闭 dry_run"})
    return alerts


@route("GET", r"/api/settings")
def settings_get(app: PanelApp, req: Request) -> Any:
    return {"settings": app.store.all_settings(),
            "auto_inspect": app.store.get_setting("inspector.auto", True)}


@route("PUT", r"/api/settings")
def settings_put(app: PanelApp, req: Request) -> Any:
    data = req.json()
    for key in ("inspector.auto", "ui.theme", "ui.page_size"):
        if key in data:
            app.store.set_setting(key, data[key])
    return {"ok": True, "settings": app.store.all_settings()}


@route("POST", r"/api/notify/test")
def notify_test(app: PanelApp, req: Request) -> Any:
    return app.notifier.test()


@route("POST", r"/api/maintenance/prune")
def maintenance_prune(app: PanelApp, req: Request) -> Any:
    data = req.json() if req.body else {}
    result = app.store.prune(usage_days=as_int(data.get("usage_days"), 180) or 180,
                             sample_days=as_int(data.get("sample_days"), 30) or 30,
                             audit_days=as_int(data.get("audit_days"), 90) or 90)
    app.store.audit("maintenance.prune", actor=req.user or "?", detail=jdump(result),
                    ip=req.client_ip)
    return {"ok": True, "deleted": result}


@route("GET", r"/api/health")
def health(app: PanelApp, req: Request) -> Any:
    stats = app.store.stats()
    return {"ok": True, "app": APP_NAME, "version": __version__,
            "uptime_seconds": now_ts() - app.started_at, "database": {
                "credentials": stats["credentials"], "usage_events": stats["usage_events"],
                "size_bytes": stats["database_bytes"]}}


@route("GET", r"/metrics")
def metrics(app: PanelApp, req: Request) -> Any:
    stats = app.store.stats()
    counts = app.store.credential_counts()
    summary = app.store.usage_summary(1)
    lines = [
        "# HELP cpa_panel_up 面板是否存活",
        "# TYPE cpa_panel_up gauge",
        "cpa_panel_up 1",
        "# HELP cpa_panel_credentials 凭证数量（按状态）",
        "# TYPE cpa_panel_credentials gauge",
        f'cpa_panel_credentials{{state="total"}} {counts.get("total", 0)}',
        f'cpa_panel_credentials{{state="active"}} {counts.get("active", 0)}',
        f'cpa_panel_credentials{{state="cooling"}} {counts.get("cooling", 0)}',
        f'cpa_panel_credentials{{state="disabled"}} {counts.get("disabled", 0)}',
        f'cpa_panel_credentials{{state="erroring"}} {counts.get("erroring", 0)}',
        "# HELP cpa_panel_usage_today 今日用量",
        "# TYPE cpa_panel_usage_today gauge",
        f'cpa_panel_usage_today{{metric="requests"}} {_summary_get(summary, "requests")}',
        f'cpa_panel_usage_today{{metric="errors"}} {_summary_get(summary, "errors")}',
        f'cpa_panel_usage_today{{metric="tokens"}} {_summary_get(summary, "total_tokens")}',
        f'cpa_panel_usage_today{{metric="cost_usd"}} {_summary_get(summary, "cost_usd")}',
        "# HELP cpa_panel_usage_events_total 已持久化的用量事件总数",
        "# TYPE cpa_panel_usage_events_total counter",
        f"cpa_panel_usage_events_total {stats.get('usage_events', 0)}",
    ]
    return 200, ("\n".join(lines) + "\n").encode("utf-8"), \
        {"Content-Type": "text/plain; version=0.0.4; charset=utf-8"}


def _summary_get(summary: Dict[str, Any], key: str) -> Any:
    return summary.get(key, 0)


# --------------------------------------------------------------------------- 工具


def _parse_multipart(req: Request) -> Tuple[List[Tuple[str, bytes]], Dict[str, str]]:
    """极简 multipart 解析（只处理文件 + 普通字段，够用且不引入依赖）。"""
    ctype = req.content_type
    if "multipart/form-data" not in ctype:
        raise ApiError("需要 multipart/form-data", 400)
    match = re.search(r"boundary=([^;]+)", ctype)
    if not match:
        raise ApiError("缺少 multipart boundary", 400)
    boundary = match.group(1).strip().strip('"').encode()
    files: List[Tuple[str, bytes]] = []
    fields: Dict[str, str] = {}
    for part in req.body.split(b"--" + boundary):
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue
        head, _, content = part.partition(b"\r\n\r\n")
        if not _:
            continue
        content = content.rstrip(b"\r\n")
        header_text = head.decode("utf-8", "replace")
        filename_match = re.search(r'filename="([^"]*)"', header_text)
        name_match = re.search(r'name="([^"]*)"', header_text)
        field_name = name_match.group(1) if name_match else ""
        if filename_match and filename_match.group(1):
            files.append((os.path.basename(filename_match.group(1)), content))
        elif field_name:
            fields[field_name] = content.decode("utf-8", "replace")
    return files, fields


# --------------------------------------------------------------------------- 启动


def serve(app: PanelApp, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundPanelHandler", (PanelHandler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def run_forever(app: PanelApp, host: str, port: int) -> None:
    httpd = serve(app, host, port)
    log.info("%s 监听 http://%s:%s （数据目录 %s）", APP_NAME, host, port, app.config.data_dir())
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("收到中断，正在退出…")
    finally:
        httpd.shutdown()
        httpd.server_close()
