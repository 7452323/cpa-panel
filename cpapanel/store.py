"""SQLite 存储层：建表、迁移、以及全部 DAO。

为什么用 SQLite：面板要回答「钱花在哪、哪个号在坏、什么时候坏的」，
这需要**持久化历史**，而上游的用量队列只有 60 秒生命期。
从上游 pop 出来的每一条记录，必须由这里负责永久保存。
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .log import get
from .util import day_of, ensure_dir, jdump, jload, now_ts

log = get("cpapanel.store")

SCHEMA_VERSION = 1

SCHEMA_STATEMENTS: List[str] = [
    """CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)""",
    """CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT)""",
    """CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'admin',
        created_at INTEGER NOT NULL,
        last_login_at INTEGER,
        disabled INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        ip TEXT, user_agent TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS panel_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL DEFAULT 'viewer',
        created_at INTEGER NOT NULL,
        last_used_at INTEGER,
        revoked INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        base_url TEXT NOT NULL,
        management_key TEXT NOT NULL DEFAULT '',
        api_prefix TEXT NOT NULL DEFAULT 'auto',
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        last_ok_at INTEGER,
        last_error TEXT,
        detected_prefix TEXT,
        version TEXT,
        UNIQUE(base_url, name)
    )""",
    """CREATE TABLE IF NOT EXISTS credentials (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id INTEGER NOT NULL,
        auth_index TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL,
        provider TEXT NOT NULL DEFAULT '',
        email TEXT, account TEXT, account_type TEXT, project_id TEXT, plan_type TEXT,
        status TEXT NOT NULL DEFAULT '',
        status_message TEXT,
        disabled INTEGER NOT NULL DEFAULT 0,
        unavailable INTEGER NOT NULL DEFAULT 0,
        runtime_only INTEGER NOT NULL DEFAULT 0,
        source TEXT, path TEXT,
        priority INTEGER, weight INTEGER, note TEXT,
        websockets INTEGER, request_retry INTEGER,
        last_refresh INTEGER, next_retry_after INTEGER, subscription_until INTEGER,
        success INTEGER NOT NULL DEFAULT 0,
        failed INTEGER NOT NULL DEFAULT 0,
        recent_requests_json TEXT, quota_json TEXT, model_quotas_json TEXT,
        id_token_json TEXT, raw_json TEXT,
        standby INTEGER NOT NULL DEFAULT 0,
        present INTEGER NOT NULL DEFAULT 1,
        first_seen_at INTEGER NOT NULL,
        last_seen_at INTEGER NOT NULL,
        last_change_at INTEGER NOT NULL,
        UNIQUE(node_id, name, auth_index)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_credentials_node ON credentials(node_id, provider, status)""",
    """CREATE TABLE IF NOT EXISTS credential_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        credential_id INTEGER NOT NULL,
        node_id INTEGER NOT NULL,
        ts INTEGER NOT NULL,
        status TEXT, disabled INTEGER, unavailable INTEGER,
        success INTEGER, failed INTEGER,
        next_retry_after INTEGER, reason TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS idx_samples_cred ON credential_samples(credential_id, ts)""",
    """CREATE TABLE IF NOT EXISTS credential_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        node_id INTEGER NOT NULL,
        credential_id INTEGER,
        credential_name TEXT,
        kind TEXT NOT NULL,
        detail TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS idx_cred_events_ts ON credential_events(ts)""",
    """CREATE TABLE IF NOT EXISTS usage_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id INTEGER NOT NULL,
        dedupe_key TEXT NOT NULL UNIQUE,
        ts INTEGER NOT NULL,
        day TEXT NOT NULL,
        model TEXT, provider TEXT,
        credential_index TEXT, credential_label TEXT,
        api_key TEXT, endpoint TEXT,
        status TEXT, http_status INTEGER,
        latency_ms INTEGER,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        reasoning_tokens INTEGER NOT NULL DEFAULT 0,
        cached_tokens INTEGER NOT NULL DEFAULT 0,
        total_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        is_error INTEGER NOT NULL DEFAULT 0,
        error TEXT, raw_json TEXT,
        collected_at INTEGER NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts DESC)""",
    """CREATE INDEX IF NOT EXISTS idx_usage_day ON usage_events(day, model)""",
    """CREATE TABLE IF NOT EXISTS usage_daily (
        day TEXT NOT NULL,
        node_id INTEGER NOT NULL,
        model TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT '',
        credential_index TEXT NOT NULL DEFAULT '',
        api_key TEXT NOT NULL DEFAULT '',
        requests INTEGER NOT NULL DEFAULT 0,
        errors INTEGER NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        reasoning_tokens INTEGER NOT NULL DEFAULT 0,
        cached_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (day, node_id, model, provider, credential_index, api_key)
    )""",
    """CREATE TABLE IF NOT EXISTS api_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id INTEGER NOT NULL,
        key_masked TEXT NOT NULL,
        key_hash TEXT NOT NULL,
        raw TEXT,
        present INTEGER NOT NULL DEFAULT 1,
        first_seen_at INTEGER NOT NULL,
        last_seen_at INTEGER NOT NULL,
        UNIQUE(node_id, key_hash)
    )""",
    """CREATE TABLE IF NOT EXISTS key_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id INTEGER NOT NULL,
        key_masked TEXT NOT NULL,
        key_hash TEXT NOT NULL,
        requests INTEGER NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        raw_json TEXT,
        updated_at INTEGER NOT NULL,
        UNIQUE(node_id, key_hash)
    )""",
    """CREATE TABLE IF NOT EXISTS inspections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id INTEGER NOT NULL,
        mode TEXT NOT NULL,
        started_at INTEGER NOT NULL,
        finished_at INTEGER,
        scanned INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 0,
        unauthorized INTEGER NOT NULL DEFAULT 0,
        quota_exhausted INTEGER NOT NULL DEFAULT 0,
        cooling INTEGER NOT NULL DEFAULT 0,
        disabled INTEGER NOT NULL DEFAULT 0,
        standby INTEGER NOT NULL DEFAULT 0,
        planned INTEGER NOT NULL DEFAULT 0,
        executed INTEGER NOT NULL DEFAULT 0,
        failures INTEGER NOT NULL DEFAULT 0,
        summary_json TEXT,
        error TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inspection_id INTEGER,
        node_id INTEGER NOT NULL,
        ts INTEGER NOT NULL,
        credential_name TEXT,
        action TEXT NOT NULL,
        reason TEXT,
        result TEXT,
        detail TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(ts DESC)""",
    """CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        actor TEXT,
        action TEXT NOT NULL,
        target TEXT,
        detail TEXT,
        ip TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC)""",
]


class Store:
    def __init__(self, path: str):
        self.path = path
        ensure_dir(os.path.dirname(os.path.abspath(path)))
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    # ------------------------------------------------------------------ 基础

    def close(self) -> None:
        with self._lock:
            with contextlib.suppress(Exception):
                self.conn.commit()
            with contextlib.suppress(Exception):
                self.conn.close()

    @contextlib.contextmanager
    def tx(self):
        with self._lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def q(self, sql: str, args: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, args).fetchall())

    def q1(self, sql: str, args: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def ex(self, sql: str, args: Sequence[Any] = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.rowcount

    def init_schema(self) -> None:
        with self.tx() as conn:
            for stmt in SCHEMA_STATEMENTS:
                conn.execute(stmt)
            current = self.get_meta("schema_version")
            if current is None:
                conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)",
                             ("schema_version", str(SCHEMA_VERSION)))
            else:
                # 未来的迁移在此处按版本顺序追加
                if int(current) < SCHEMA_VERSION:
                    conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)",
                                 ("schema_version", str(SCHEMA_VERSION)))
                log.debug("schema 版本 %s（当前代码 %s）", current, SCHEMA_VERSION)

    # ------------------------------------------------------------------ meta/settings

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.q1("SELECT v FROM meta WHERE k = ?", (key,))
        return row["v"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.ex("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (key, str(value)))

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.q1("SELECT v FROM settings WHERE k = ?", (key,))
        if row is None:
            return default
        return jload(row["v"], default)

    def set_setting(self, key: str, value: Any) -> None:
        self.ex("INSERT OR REPLACE INTO settings(k, v) VALUES(?, ?)", (key, jdump(value)))

    def all_settings(self) -> Dict[str, Any]:
        return {r["k"]: jload(r["v"]) for r in self.q("SELECT k, v FROM settings")}

    # ------------------------------------------------------------------ 用户/会话

    def create_user(self, username: str, password_hash: str, role: str = "admin") -> int:
        ts = now_ts()
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
                (username, password_hash, role, ts))
            return int(cur.lastrowid)

    def upsert_user(self, username: str, password_hash: str, role: str = "admin") -> int:
        existing = self.q1("SELECT id FROM users WHERE username = ?", (username,))
        if existing:
            self.ex("UPDATE users SET password_hash = ?, role = ? WHERE id = ?",
                    (password_hash, role, existing["id"]))
            return int(existing["id"])
        return self.create_user(username, password_hash, role)

    def get_user(self, username: str) -> Optional[sqlite3.Row]:
        return self.q1("SELECT * FROM users WHERE username = ?", (username,))

    def touch_login(self, user_id: int) -> None:
        self.ex("UPDATE users SET last_login_at = ? WHERE id = ?", (now_ts(), user_id))

    def set_user_password(self, username: str, password_hash: str) -> None:
        self.ex("UPDATE users SET password_hash = ? WHERE username = ?", (password_hash, username))

    def create_session(self, token: str, user_id: int, ttl: int, ip: str, ua: str) -> None:
        ts = now_ts()
        self.ex("INSERT INTO sessions(token, user_id, created_at, expires_at, ip, user_agent) "
                "VALUES(?,?,?,?,?,?)", (token, user_id, ts, ts + int(ttl), ip, ua))

    def get_session(self, token: str) -> Optional[sqlite3.Row]:
        row = self.q1("SELECT * FROM sessions WHERE token = ?", (token,))
        if not row:
            return None
        if int(row["expires_at"]) < now_ts():
            self.ex("DELETE FROM sessions WHERE token = ?", (token,))
            return None
        return row

    def delete_session(self, token: str) -> None:
        self.ex("DELETE FROM sessions WHERE token = ?", (token,))

    def purge_sessions(self) -> int:
        return self.ex("DELETE FROM sessions WHERE expires_at < ?", (now_ts(),))

    # ------------------------------------------------------------------ 面板令牌

    def create_panel_token(self, name: str, token_hash: str, role: str = "viewer") -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO panel_tokens(name, token_hash, role, created_at) VALUES(?,?,?,?)",
                (name, token_hash, role, now_ts()))
            return int(cur.lastrowid)

    def find_panel_token(self, token_hash: str) -> Optional[sqlite3.Row]:
        return self.q1("SELECT * FROM panel_tokens WHERE token_hash = ? AND revoked = 0", (token_hash,))

    def touch_panel_token(self, token_id: int) -> None:
        self.ex("UPDATE panel_tokens SET last_used_at = ? WHERE id = ?", (now_ts(), token_id))

    def list_panel_tokens(self) -> List[sqlite3.Row]:
        return self.q("SELECT id, name, role, created_at, last_used_at, revoked FROM panel_tokens "
                      "ORDER BY id DESC")

    def revoke_panel_token(self, token_id: int) -> None:
        self.ex("UPDATE panel_tokens SET revoked = 1 WHERE id = ?", (token_id,))

    # ------------------------------------------------------------------ 节点

    def add_node(self, name: str, base_url: str, management_key: str, api_prefix: str = "auto",
                 enabled: bool = True) -> int:
        ts = now_ts()
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO nodes(name, base_url, management_key, api_prefix, enabled, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (name, base_url.rstrip("/"), management_key, api_prefix, 1 if enabled else 0, ts, ts))
            return int(cur.lastrowid)

    def ensure_bootstrap_nodes(self, items: Iterable[Dict[str, Any]]) -> List[int]:
        """把配置文件里的引导节点导入数据库（幂等）。"""
        created: List[int] = []
        for item in items:
            base = str(item.get("base_url") or "").rstrip("/")
            if not base:
                continue
            key = item.get("management_key") or ""
            existing = self.q1("SELECT id FROM nodes WHERE base_url = ?", (base,))
            if existing:
                # 密钥以数据库为准，但配置文件里若填了新的则更新
                if key:
                    self.ex("UPDATE nodes SET management_key = ?, updated_at = ? WHERE id = ?",
                            (key, now_ts(), existing["id"]))
                continue
            created.append(self.add_node(item.get("name") or "default", base, key,
                                         item.get("api_prefix") or "auto"))
        return created

    def list_nodes(self, only_enabled: bool = False) -> List[sqlite3.Row]:
        sql = "SELECT * FROM nodes"
        if only_enabled:
            sql += " WHERE enabled = 1"
        return self.q(sql + " ORDER BY id")

    def get_node(self, node_id: int) -> Optional[sqlite3.Row]:
        return self.q1("SELECT * FROM nodes WHERE id = ?", (node_id,))

    def update_node(self, node_id: int, **fields: Any) -> None:
        allowed = {"name", "base_url", "management_key", "api_prefix", "enabled",
                   "last_ok_at", "last_error", "detected_prefix", "version"}
        sets, args = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            args.append(value)
        if not sets:
            return
        sets.append("updated_at = ?")
        args.append(now_ts())
        args.append(node_id)
        self.ex(f"UPDATE nodes SET {', '.join(sets)} WHERE id = ?", args)

    def delete_node(self, node_id: int) -> None:
        with self.tx() as conn:
            conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
            conn.execute("DELETE FROM credentials WHERE node_id = ?", (node_id,))
            conn.execute("DELETE FROM credential_samples WHERE node_id = ?", (node_id,))
            conn.execute("DELETE FROM api_keys WHERE node_id = ?", (node_id,))
            conn.execute("DELETE FROM key_usage WHERE node_id = ?", (node_id,))
            # usage_events / usage_daily 保留（历史账目不应因移除节点而消失）

    # ------------------------------------------------------------------ 凭证

    CRED_FIELDS = ("auth_index", "name", "provider", "email", "account", "account_type",
                   "project_id", "plan_type", "status", "status_message", "disabled",
                   "unavailable", "runtime_only", "source", "path", "priority", "weight",
                   "note", "websockets", "request_retry", "last_refresh", "next_retry_after",
                   "subscription_until", "success", "failed", "recent_requests_json",
                   "quota_json", "model_quotas_json", "id_token_json", "raw_json")

    def upsert_credential(self, node_id: int, item: Dict[str, Any]) -> Tuple[int, List[Dict[str, Any]]]:
        """写入/更新一个凭证快照，返回 (credential_id, 变更列表)。

        变更检测是面板的核心价值之一：它把「上游某一时刻的快照」变成「有时间轴的事件」。
        """
        ts = now_ts()
        name = item.get("name") or ""
        auth_index = item.get("auth_index") or ""
        row = self.q1("SELECT * FROM credentials WHERE node_id = ? AND name = ? AND auth_index = ?",
                      (node_id, name, auth_index))
        changes: List[Dict[str, Any]] = []
        payload = {k: item.get(k) for k in self.CRED_FIELDS if k in item}
        payload["name"] = name
        payload["auth_index"] = auth_index

        if row is None:
            cols = list(payload.keys()) + ["node_id", "first_seen_at", "last_seen_at", "last_change_at", "present"]
            args = list(payload.values()) + [node_id, ts, ts, ts, 1]
            placeholders = ",".join("?" * len(cols))
            with self.tx() as conn:
                cur = conn.execute(f"INSERT INTO credentials({','.join(cols)}) VALUES({placeholders})", args)
                cid = int(cur.lastrowid)
            changes.append({"kind": "added", "name": name, "detail": item.get("status")})
            self.add_credential_event(node_id, cid, name, "added",
                                      f"provider={payload.get('provider')} status={payload.get('status')}")
            return cid, changes

        cid = int(row["id"])
        # 逐字段比较，产出人类可读的变更
        for key, value in payload.items():
            old = row[key] if key in row.keys() else None
            if key == "raw_json":
                continue
            if _norm(old) != _norm(value):
                if key in ("status", "disabled", "unavailable", "status_message"):
                    changes.append({
                        "kind": "state_changed", "name": name, "field": key,
                        "from": old, "to": value,
                    })
        if changes:
            self.add_credential_event(node_id, cid, name, "state_changed", jdump(changes))

        sets_sql = ", ".join(f"{k} = ?" for k in payload)
        self.ex(
            f"UPDATE credentials SET {sets_sql}, last_seen_at = ?, present = 1, "
            f"last_change_at = CASE WHEN ? THEN ? ELSE last_change_at END WHERE id = ?",
            list(payload.values()) + [ts, 1 if changes else 0, ts, cid])
        return cid, changes

    def sync_credentials(self, node_id: int, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """整体同步：写入快照 + 标记消失 + 汇总变更。

        返回 {added, changed, removed, unchanged, ids}
        """
        ts = now_ts()
        seen: List[Tuple[str, str]] = []
        added = changed = 0
        changes_all: List[Dict[str, Any]] = []
        for item in items:
            name = item.get("name") or ""
            auth_index = item.get("auth_index") or ""
            seen.append((name, auth_index))
            _cid, changes = self.upsert_credential(node_id, item)
            if changes:
                kinds = {c["kind"] for c in changes}
                if "added" in kinds:
                    added += 1
                else:
                    changed += 1
                changes_all.extend(changes)
        removed = 0
        existing = self.q("SELECT id, name, auth_index FROM credentials WHERE node_id = ? AND present = 1",
                          (node_id,))
        seen_set = set(seen)
        for row in existing:
            if (row["name"], row["auth_index"]) not in seen_set:
                self.ex("UPDATE credentials SET present = 0, last_seen_at = ? WHERE id = ?", (ts, row["id"]))
                self.add_credential_event(node_id, int(row["id"]), row["name"], "removed", "不再出现在上游列表")
                removed += 1
        unchanged = max(0, len(items) - added - changed)
        return {"added": added, "changed": changed, "removed": removed,
                "unchanged": unchanged, "total": len(items), "changes": changes_all}

    def add_credential_event(self, node_id: int, credential_id: Optional[int], name: str,
                             kind: str, detail: str = "") -> None:
        self.ex("INSERT INTO credential_events(ts, node_id, credential_id, credential_name, kind, detail) "
                "VALUES(?,?,?,?,?,?)", (now_ts(), node_id, credential_id, name, kind, detail))

    def list_credentials(self, node_id: Optional[int] = None, provider: str = "",
                         status: str = "", keyword: str = "", present_only: bool = True,
                         standby: Optional[bool] = None, limit: int = 2000) -> List[sqlite3.Row]:
        sql = "SELECT * FROM credentials WHERE 1=1"
        args: List[Any] = []
        if node_id:
            sql += " AND node_id = ?"
            args.append(node_id)
        if present_only:
            sql += " AND present = 1"
        if provider:
            sql += " AND provider = ?"
            args.append(provider)
        if status:
            sql += " AND status = ?"
            args.append(status)
        if standby is not None:
            sql += " AND standby = ?"
            args.append(1 if standby else 0)
        if keyword:
            sql += " AND (name LIKE ? OR email LIKE ? OR account LIKE ? OR note LIKE ?)"
            like = f"%{keyword}%"
            args.extend([like, like, like, like])
        sql += " ORDER BY provider, name LIMIT ?"
        args.append(limit)
        return self.q(sql, args)

    def get_credential(self, cred_id: int) -> Optional[sqlite3.Row]:
        return self.q1("SELECT * FROM credentials WHERE id = ?", (cred_id,))

    def set_credential_standby(self, cred_id: int, standby: bool) -> None:
        self.ex("UPDATE credentials SET standby = ?, last_change_at = ? WHERE id = ?",
                (1 if standby else 0, now_ts(), cred_id))

    def mark_credential_deleted(self, cred_id: int) -> None:
        self.ex("UPDATE credentials SET present = 0, last_change_at = ? WHERE id = ?", (now_ts(), cred_id))

    def credential_counts(self, node_id: Optional[int] = None) -> Dict[str, int]:
        sql = ("SELECT COUNT(*) AS total, "
               "SUM(CASE WHEN disabled = 1 THEN 1 ELSE 0 END) AS disabled, "
               "SUM(CASE WHEN unavailable = 1 AND disabled = 0 THEN 1 ELSE 0 END) AS cooling, "
               "SUM(CASE WHEN standby = 1 THEN 1 ELSE 0 END) AS standby, "
               "SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS erroring "
               "FROM credentials WHERE present = 1")
        args: List[Any] = []
        if node_id:
            sql += " AND node_id = ?"
            args.append(node_id)
        row = self.q1(sql, args)
        if not row:
            return {"total": 0, "disabled": 0, "cooling": 0, "standby": 0, "erroring": 0}
        out = {k: int(row[k] or 0) for k in ("total", "disabled", "cooling", "standby", "erroring")}
        out["active"] = max(0, out["total"] - out["disabled"] - out["cooling"])
        return out

    def provider_breakdown(self, node_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = ("SELECT provider, COUNT(*) AS total, "
               "SUM(CASE WHEN disabled = 0 AND unavailable = 0 AND status != 'error' THEN 1 ELSE 0 END) AS healthy "
               "FROM credentials WHERE present = 1")
        args: List[Any] = []
        if node_id:
            sql += " AND node_id = ?"
            args.append(node_id)
        sql += " GROUP BY provider ORDER BY total DESC"
        return [dict(r) for r in self.q(sql, args)]

    def add_credential_sample(self, cred_id: int, node_id: int, ts: int, status: str,
                              disabled: bool, unavailable: bool, success: int, failed: int,
                              next_retry_after: Optional[int], reason: str = "") -> None:
        self.ex("INSERT INTO credential_samples(credential_id, node_id, ts, status, disabled, "
                "unavailable, success, failed, next_retry_after, reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (cred_id, node_id, ts, status, 1 if disabled else 0, 1 if unavailable else 0,
                 int(success or 0), int(failed or 0), next_retry_after, reason))

    def credential_samples(self, cred_id: int, limit: int = 200) -> List[Dict[str, Any]]:
        rows = self.q("SELECT * FROM credential_samples WHERE credential_id = ? ORDER BY ts DESC LIMIT ?",
                      (cred_id, limit))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ 用量

    def insert_usage_event(self, node_id: int, event: Dict[str, Any]) -> bool:
        """幂等写入一条用量记录；返回 True 表示是新记录。

        队列是消费型的，所以「重复」只可能来自重试，不该重复计费。
        """
        try:
            with self.tx() as conn:
                conn.execute(
                    "INSERT INTO usage_events(node_id, dedupe_key, ts, day, model, provider, "
                    "credential_index, credential_label, api_key, endpoint, status, http_status, "
                    "latency_ms, input_tokens, output_tokens, reasoning_tokens, cached_tokens, "
                    "total_tokens, cost_usd, is_error, error, raw_json, collected_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (node_id, event["dedupe_key"], event["ts"], event.get("day") or day_of(event["ts"]),
                     event.get("model"), event.get("provider"), event.get("credential_index"),
                     event.get("credential_label"), event.get("api_key"), event.get("endpoint"),
                     event.get("status"), event.get("http_status"), event.get("latency_ms"),
                     int(event.get("input_tokens") or 0), int(event.get("output_tokens") or 0),
                     int(event.get("reasoning_tokens") or 0), int(event.get("cached_tokens") or 0),
                     int(event.get("total_tokens") or 0), float(event.get("cost_usd") or 0.0),
                     1 if event.get("is_error") else 0, event.get("error"), event.get("raw_json"),
                     now_ts()))
            return True
        except sqlite3.IntegrityError:
            return False

    def aggregate_event(self, event: Dict[str, Any]) -> None:
        """把单条记录累加到 usage_daily（增量聚合，查询时无需全表扫描）。"""
        args = (event.get("day") or day_of(event["ts"]), event.get("node_id") or event.get("_node_id") or 0,
                event.get("model") or "", event.get("provider") or "",
                event.get("credential_index") or "", event.get("api_key") or "")
        with self.tx() as conn:
            conn.execute("INSERT OR IGNORE INTO usage_daily(day, node_id, model, provider, "
                         "credential_index, api_key) VALUES(?,?,?,?,?,?)", args)
            conn.execute(
                "UPDATE usage_daily SET requests = requests + 1, errors = errors + ?, "
                "input_tokens = input_tokens + ?, output_tokens = output_tokens + ?, "
                "reasoning_tokens = reasoning_tokens + ?, cached_tokens = cached_tokens + ?, "
                "cost_usd = cost_usd + ? "
                "WHERE day = ? AND node_id = ? AND model = ? AND provider = ? "
                "AND credential_index = ? AND api_key = ?",
                (1 if event.get("is_error") else 0, int(event.get("input_tokens") or 0),
                 int(event.get("output_tokens") or 0), int(event.get("reasoning_tokens") or 0),
                 int(event.get("cached_tokens") or 0), float(event.get("cost_usd") or 0.0)) + args)

    def usage_summary(self, days: int = 7, node_id: Optional[int] = None) -> Dict[str, Any]:
        since = day_of(now_ts() - days * 86400)
        sql = ("SELECT SUM(requests) AS requests, SUM(errors) AS errors, "
               "SUM(input_tokens) AS input_tokens, "
               "SUM(output_tokens) AS output_tokens, SUM(reasoning_tokens) AS reasoning_tokens, "
               "SUM(cached_tokens) AS cached_tokens, SUM(cost_usd) AS cost_usd "
               "FROM usage_daily WHERE day >= ?")
        args: List[Any] = [since]
        if node_id:
            sql += " AND node_id = ?"
            args.append(node_id)
        row = self.q1(sql, args)
        data = {k: (row[k] if row and row[k] is not None else 0) for k in
                ("requests", "errors", "input_tokens", "output_tokens", "reasoning_tokens",
                 "cached_tokens", "cost_usd")}
        data["days"] = days
        data["since"] = since
        data["total_tokens"] = data["input_tokens"] + data["output_tokens"] + data["reasoning_tokens"]
        req = data["requests"] or 0
        data["error_rate"] = round((data["errors"] or 0) / req, 4) if req else 0.0
        return data

    def usage_series(self, days: int = 14, node_id: Optional[int] = None) -> List[Dict[str, Any]]:
        since = day_of(now_ts() - days * 86400)
        sql = ("SELECT day, SUM(requests) AS requests, SUM(errors) AS errors, "
               "SUM(input_tokens + output_tokens + reasoning_tokens) AS tokens, SUM(cost_usd) AS cost_usd "
               "FROM usage_daily WHERE day >= ?")
        args: List[Any] = [since]
        if node_id:
            sql += " AND node_id = ?"
            args.append(node_id)
        sql += " GROUP BY day ORDER BY day"
        return [dict(r) for r in self.q(sql, args)]

    def usage_by_model(self, days: int = 7, limit: int = 20, node_id: Optional[int] = None) -> List[Dict[str, Any]]:
        since = day_of(now_ts() - days * 86400)
        sql = ("SELECT model, SUM(requests) AS requests, SUM(errors) AS errors, "
               "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
               "SUM(reasoning_tokens) AS reasoning_tokens, SUM(cached_tokens) AS cached_tokens, "
               "SUM(cost_usd) AS cost_usd FROM usage_daily WHERE day >= ? AND model != ''")
        args: List[Any] = [since]
        if node_id:
            sql += " AND node_id = ?"
            args.append(node_id)
        sql += " GROUP BY model ORDER BY cost_usd DESC, requests DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.q(sql, args)]

    def usage_by_credential(self, days: int = 7, limit: int = 50,
                            node_id: Optional[int] = None) -> List[Dict[str, Any]]:
        since = day_of(now_ts() - days * 86400)
        sql = ("SELECT credential_index, SUM(requests) AS requests, SUM(errors) AS errors, "
               "SUM(input_tokens + output_tokens + reasoning_tokens) AS tokens, SUM(cost_usd) AS cost_usd "
               "FROM usage_daily WHERE day >= ? AND credential_index != ''")
        args: List[Any] = [since]
        if node_id:
            sql += " AND node_id = ?"
            args.append(node_id)
        sql += " GROUP BY credential_index ORDER BY requests DESC LIMIT ?"
        args.append(limit)
        rows = [dict(r) for r in self.q(sql, args)]
        # 关联凭证名称，便于展示
        for item in rows:
            cred = self.q1("SELECT name, provider, email FROM credentials WHERE auth_index = ? LIMIT 1",
                           (item["credential_index"],))
            if cred:
                item["name"] = cred["name"]
                item["provider"] = cred["provider"]
                item["email"] = cred["email"]
            else:
                item["name"] = item["credential_index"]
        return rows

    def usage_by_key(self, days: int = 7, node_id: Optional[int] = None) -> List[Dict[str, Any]]:
        since = day_of(now_ts() - days * 86400)
        sql = ("SELECT api_key, SUM(requests) AS requests, SUM(errors) AS errors, "
               "SUM(input_tokens + output_tokens + reasoning_tokens) AS tokens, SUM(cost_usd) AS cost_usd "
               "FROM usage_daily WHERE day >= ? AND api_key != ''")
        args: List[Any] = [since]
        if node_id:
            sql += " AND node_id = ?"
            args.append(node_id)
        sql += " GROUP BY api_key ORDER BY requests DESC LIMIT 100"
        rows = [dict(r) for r in self.q(sql, args)]
        for item in rows:
            item["api_key_masked"] = _mask_key(item["api_key"])
        return rows

    def list_usage_events(self, node_id: Optional[int] = None, model: str = "", credential: str = "",
                          api_key: str = "", errors_only: bool = False, since: Optional[int] = None,
                          limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM usage_events WHERE 1=1"
        args: List[Any] = []
        if node_id:
            sql += " AND node_id = ?"
            args.append(node_id)
        if model:
            sql += " AND model = ?"
            args.append(model)
        if credential:
            sql += " AND credential_index = ?"
            args.append(credential)
        if api_key:
            sql += " AND api_key = ?"
            args.append(api_key)
        if errors_only:
            sql += " AND is_error = 1"
        if since:
            sql += " AND ts >= ?"
            args.append(int(since))
        sql += " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?"
        args.extend([int(limit), int(offset)])
        rows = [dict(r) for r in self.q(sql, args)]
        for item in rows:
            item["api_key_masked"] = _mask_key(item.get("api_key"))
        return rows

    def usage_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        row = self.q1("SELECT * FROM usage_events WHERE id = ?", (event_id,))
        if not row:
            return None
        item = dict(row)
        item["api_key_masked"] = _mask_key(item.get("api_key"))
        return item

    def usage_totals_by_day(self, node_id: Optional[int] = None) -> Dict[str, Any]:
        row = self.q1("SELECT COUNT(*) AS events, MIN(ts) AS first_ts, MAX(ts) AS last_ts "
                      "FROM usage_events" + (" WHERE node_id = ?" if node_id else ""),
                      (node_id,) if node_id else ())
        return dict(row) if row else {"events": 0, "first_ts": None, "last_ts": None}

    # ------------------------------------------------------------------ 下游 Key

    def sync_api_keys(self, node_id: int, keys: List[str]) -> Dict[str, int]:
        ts = now_ts()
        added = 0
        keep_hashes = []
        for key in keys:
            if not key:
                continue
            kh = _sha1(key)
            keep_hashes.append(kh)
            row = self.q1("SELECT id FROM api_keys WHERE node_id = ? AND key_hash = ?", (node_id, kh))
            if row:
                self.ex("UPDATE api_keys SET present = 1, last_seen_at = ?, raw = ? WHERE id = ?",
                        (ts, _mask_key(key), row["id"]))
            else:
                self.ex("INSERT INTO api_keys(node_id, key_masked, key_hash, raw, present, "
                        "first_seen_at, last_seen_at) VALUES(?,?,?,?,1,?,?)",
                        (node_id, _mask_key(key), kh, _mask_key(key), ts, ts))
                added += 1
        removed = 0
        for row in self.q("SELECT id, key_hash FROM api_keys WHERE node_id = ? AND present = 1", (node_id,)):
            if row["key_hash"] not in keep_hashes:
                self.ex("UPDATE api_keys SET present = 0, last_seen_at = ? WHERE id = ?", (ts, row["id"]))
                removed += 1
        return {"added": added, "removed": removed, "total": len(keep_hashes)}

    def list_api_keys(self, node_id: Optional[int] = None, present_only: bool = True) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM api_keys WHERE 1=1"
        args: List[Any] = []
        if node_id:
            sql += " AND node_id = ?"
            args.append(node_id)
        if present_only:
            sql += " AND present = 1"
        sql += " ORDER BY id"
        rows = [dict(r) for r in self.q(sql, args)]
        usage = {(u["node_id"], u["key_hash"]): u for u in
                 [dict(r) for r in self.q("SELECT * FROM key_usage")]}
        for item in rows:
            u = usage.get((item["node_id"], item["key_hash"]))
            item["usage"] = {
                "requests": u["requests"] if u else 0,
                "input_tokens": u["input_tokens"] if u else 0,
                "output_tokens": u["output_tokens"] if u else 0,
                "cost_usd": u["cost_usd"] if u else 0.0,
                "updated_at": u["updated_at"] if u else None,
            }
            item.pop("raw", None)
        return rows

    def upsert_key_usage(self, node_id: int, items: List[Dict[str, Any]]) -> int:
        ts = now_ts()
        n = 0
        for item in items:
            key = item.get("api_key") or item.get("key") or ""
            if not key:
                continue
            kh = _sha1(key)
            self.ex("INSERT INTO key_usage(node_id, key_masked, key_hash, requests, input_tokens, "
                    "output_tokens, cost_usd, raw_json, updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(node_id, key_hash) DO UPDATE SET requests = excluded.requests, "
                    "input_tokens = excluded.input_tokens, output_tokens = excluded.output_tokens, "
                    "cost_usd = excluded.cost_usd, raw_json = excluded.raw_json, "
                    "updated_at = excluded.updated_at",
                    (node_id, _mask_key(key), kh, int(item.get("requests") or 0),
                     int(item.get("input_tokens") or 0), int(item.get("output_tokens") or 0),
                     float(item.get("cost_usd") or 0.0), jdump(item.get("raw")), ts))
            n += 1
        return n

    # ------------------------------------------------------------------ 巡检

    def start_inspection(self, node_id: int, mode: str) -> int:
        with self.tx() as conn:
            cur = conn.execute("INSERT INTO inspections(node_id, mode, started_at) VALUES(?,?,?)",
                               (node_id, mode, now_ts()))
            return int(cur.lastrowid)

    def finish_inspection(self, inspection_id: int, **fields: Any) -> None:
        allowed = {"scanned", "active", "unauthorized", "quota_exhausted", "cooling", "disabled",
                   "standby", "planned", "executed", "failures", "summary_json", "error"}
        sets, args = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key} = ?")
                args.append(jdump(value) if key == "summary_json" and not isinstance(value, str) else value)
        sets.append("finished_at = ?")
        args.append(now_ts())
        args.append(inspection_id)
        self.ex(f"UPDATE inspections SET {', '.join(sets)} WHERE id = ?", args)

    def add_action(self, inspection_id: Optional[int], node_id: int, credential_name: str,
                   action: str, reason: str, result: str, detail: str = "") -> None:
        self.ex("INSERT INTO actions(inspection_id, node_id, ts, credential_name, action, reason, "
                "result, detail) VALUES(?,?,?,?,?,?,?,?)",
                (inspection_id, node_id, now_ts(), credential_name, action, reason, result, detail))

    def list_inspections(self, node_id: Optional[int] = None, limit: int = 50) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM inspections"
        args: List[Any] = []
        if node_id:
            sql += " WHERE node_id = ?"
            args.append(node_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = []
        for r in self.q(sql, args):
            item = dict(r)
            item["summary"] = jload(item.pop("summary_json", None), {})
            rows.append(item)
        return rows

    def list_actions(self, node_id: Optional[int] = None, limit: int = 100,
                     inspection_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM actions WHERE 1=1"
        args: List[Any] = []
        if node_id:
            sql += " AND node_id = ?"
            args.append(node_id)
        if inspection_id:
            sql += " AND inspection_id = ?"
            args.append(inspection_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.q(sql, args)]

    # ------------------------------------------------------------------ 审计

    def audit(self, action: str, actor: str = "system", target: str = "", detail: str = "",
              ip: str = "") -> None:
        self.ex("INSERT INTO audit(ts, actor, action, target, detail, ip) VALUES(?,?,?,?,?,?)",
                (now_ts(), actor, action, target, detail, ip))

    def list_audit(self, limit: int = 200, action: str = "") -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit"
        args: List[Any] = []
        if action:
            sql += " WHERE action LIKE ?"
            args.append(f"%{action}%")
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.q(sql, args)]

    # ------------------------------------------------------------------ 维护

    def stats(self) -> Dict[str, Any]:
        def count(table: str, where: str = "", args: Sequence[Any] = ()) -> int:
            row = self.q1(f"SELECT COUNT(*) AS n FROM {table} {where}", args)
            return int(row["n"]) if row else 0
        return {
            "nodes": count("nodes"),
            "credentials": count("credentials", "WHERE present = 1"),
            "credentials_total": count("credentials"),
            "usage_events": count("usage_events"),
            "inspections": count("inspections"),
            "actions": count("actions"),
            "audit": count("audit"),
            "database_bytes": os.path.getsize(self.path) if os.path.exists(self.path) else 0,
        }

    def prune(self, usage_days: int = 180, sample_days: int = 30, audit_days: int = 90) -> Dict[str, int]:
        """清理过期数据（用量明细保留更久，采样与审计较短）。"""
        ts_usage = now_ts() - usage_days * 86400
        ts_sample = now_ts() - sample_days * 86400
        ts_audit = now_ts() - audit_days * 86400
        out = {
            "usage_events": self.ex("DELETE FROM usage_events WHERE ts < ?", (ts_usage,)),
            "usage_daily": self.ex("DELETE FROM usage_daily WHERE day < ?", (day_of(ts_usage),)),
            "credential_samples": self.ex("DELETE FROM credential_samples WHERE ts < ?", (ts_sample,)),
            "credential_events": self.ex("DELETE FROM credential_events WHERE ts < ?", (ts_audit,)),
            "audit": self.ex("DELETE FROM audit WHERE ts < ?", (ts_audit,)),
            "sessions": self.ex("DELETE FROM sessions WHERE expires_at < ?", (now_ts(),)),
        }
        with self._lock:
            self.conn.execute("VACUUM")
            self.conn.commit()
        return out


# ---------------------------------------------------------------------- 内部

def _norm(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if value is None:
        return None
    return value


def _sha1(text: str) -> str:
    import hashlib
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()


def _mask_key(value: Optional[str]) -> str:
    if not value:
        return ""
    s = str(value)
    if len(s) <= 12:
        return s[:2] + "*" * max(0, len(s) - 4) + s[-2:]
    return f"{s[:7]}…{s[-4:]}"
