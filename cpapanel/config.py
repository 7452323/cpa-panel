"""面板配置：默认值 → 配置文件 → 环境变量 → 命令行，逐层覆盖。

设计原则：
* 配置里**允许**放敏感值（节点管理密钥、通知 token），因此文件默认 0600，且对外输出一律脱敏。
* 不发明加密：标准库没有可用的 AEAD，伪加密只会给人虚假安全感。
  真正的保护来自：文件权限 0600 + API 响应脱敏 + 日志脱敏 + 可用环境变量覆盖。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from . import APP_NAME, __version__
from .util import deep_merge, ensure_dir, jdump_pretty, jload, read_text, write_text_atomic

ENV_PREFIX = "CPAPANEL_"

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": __version__,
    # --- Web ---
    "host": "127.0.0.1",
    "port": 18317,
    "data_dir": "data",
    "database": "data/panel.db",
    "log_file": "data/panel.log",
    "log_level": "INFO",
    "public_url": "",                 # 反代场景下用于生成 OAuth 回调地址
    "trust_proxy_headers": False,
    # --- 管理员（首次 init 写入；为空则必须用 CPAPANEL_ADMIN_PASSWORD 引导） ---
    "admin": {
        "username": "admin",
        "password_hash": "",          # pbkdf2_sha256$iterations$salt$hash
        "session_ttl_seconds": 86400 * 7,
    },
    # --- 用量采集（★ 必须远小于上游 60s 的队列保留窗口） ---
    "collector": {
        "enabled": True,
        "interval_seconds": 15,
        "batch_size": 50,
        "queue_retention_seconds": 60,   # 与上游 redisqueue.defaultRetentionSeconds 对齐
        "gap_warn_ratio": 3.0,           # 相邻成功采集间隔 > retention*ratio 时告警
        "api_key_usage_every": 40,       # 每 N 轮顺带刷新一次下游 Key 用量
    },
    # --- 巡检与自动化 ---
    "inspector": {
        "enabled": True,
        "interval_seconds": 900,
        "dry_run": True,                 # ★ 默认只做计划不落动作
        "check_quota": True,             # 是否调用上游主动配额查询
        "disable_unauthorized": False,
        "disable_quota_exhausted": True,
        "delete_unauthorized": False,    # ★ 危险动作，默认关
        "delete_quota_exhausted": False,
        "max_deletes_per_run": 20,
        "standby_pool": True,            # 把失效号移入本地备用池而不是直接删
        "target_active": 0,              # >0 时不足会从备用池转活跃，并触发低水位告警
        "promote_standby_when_low": True,
        # 就绪率熔断：批量掉号时先停手，避免「集体失效 → 一轮删掉半个池子」。
        # 就绪 = 总数 - 需重登 - 额度耗尽（冷却与已禁用不算坏），低于比例则本轮只出计划。
        "circuit_breaker_enabled": True,
        "min_ready_ratio": 0.5,
        # 只有文案证据（status_message 里一句 “429 too many requests”）时，
        # 是否允许做不可逆动作（禁用/删除）。默认 **False**：先只标记 + 告警，
        # 因为瞬时限流几分钟后自己就好，而删掉的号回不来。
        "act_on_weak_evidence": False,
    },
    # --- 通知 ---
    "notify": {
        "webhook_url": "",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "events": ["inspection.unauthorized", "inspection.low_pool", "inspection.circuit_open",
                   "collector.gap", "collector.error"],
        "min_interval_seconds": 300,     # 同事件去抖
    },
    # --- 其他 ---
    "pricing_file": "pricing.json",
    "nodes": [],                         # 引导节点（首次启动时导入到数据库）
    "http": {"connect_timeout": 5, "read_timeout": 20, "verify_tls": True},
}


class Config:
    def __init__(self, path: Optional[str] = None, data: Optional[Dict[str, Any]] = None):
        self.path = path
        self.data: Dict[str, Any] = deep_merge(DEFAULT_CONFIG, data or {})

    # ---------------------------------------------------------------- 读写
    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        data: Dict[str, Any] = {}
        if path and os.path.exists(path):
            data = jload(read_text(path), {}) or {}
        elif path is None:
            candidate = os.environ.get(ENV_PREFIX + "CONFIG")
            if candidate and os.path.exists(candidate):
                path = candidate
                data = jload(read_text(candidate), {}) or {}
        cfg = cls(path=path, data=data)
        cfg.apply_env()
        return cfg

    def save(self, path: Optional[str] = None) -> str:
        target = path or self.path
        if not target:
            raise ValueError("配置路径未设置")
        self.data["version"] = __version__
        write_text_atomic(target, jdump_pretty(self.data))
        self.path = target
        return target

    def apply_env(self) -> None:
        """环境变量覆盖，便于容器化部署时不落盘密钥。"""
        mapping = {
            "HOST": ("host", str),
            "PORT": ("port", int),
            "DATA_DIR": ("data_dir", str),
            "DATABASE": ("database", str),
            "LOG_LEVEL": ("log_level", str),
            "PUBLIC_URL": ("public_url", str),
            "ADMIN_USERNAME": ("admin.username", str),
            "ADMIN_PASSWORD": ("admin.password", str),      # 明文引导，init 时哈希后不落盘
            "PRICING_FILE": ("pricing_file", str),
        }
        for env, (dotted, caster) in mapping.items():
            raw = os.environ.get(ENV_PREFIX + env)
            if raw is None:
                continue
            try:
                self.set(dotted, caster(raw))
            except (TypeError, ValueError):
                continue
        node_url = os.environ.get(ENV_PREFIX + "NODE_URL")
        node_key = os.environ.get(ENV_PREFIX + "NODE_KEY")
        if node_url:
            self.data.setdefault("nodes", [])
            if not any(n.get("base_url") == node_url for n in self.data["nodes"]):
                self.data["nodes"].append({
                    "name": os.environ.get(ENV_PREFIX + "NODE_NAME") or "default",
                    "base_url": node_url,
                    "management_key": node_key or "",
                    "api_prefix": os.environ.get(ENV_PREFIX + "NODE_PREFIX") or "auto",
                })

    # ---------------------------------------------------------------- 访问
    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        cur = self.data
        for part in parts[:-1]:
            nxt = cur.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[part] = nxt
            cur = nxt
        cur[parts[-1]] = value

    def bootstrap_admin_password(self) -> Optional[str]:
        """返回待哈希的明文密码（来自环境变量），不落盘明文。"""
        raw = os.environ.get(ENV_PREFIX + "ADMIN_PASSWORD")
        return raw or None

    # ---------------------------------------------------------------- 路径
    def resolve(self, p: str) -> str:
        if not p:
            return p
        if os.path.isabs(p):
            return p
        base = os.path.dirname(os.path.abspath(self.path)) if self.path else os.getcwd()
        return os.path.normpath(os.path.join(base, p))

    def data_dir(self) -> str:
        d = self.resolve(str(self.get("data_dir", "data")))
        ensure_dir(d)
        return d

    def database_path(self) -> str:
        return self.resolve(str(self.get("database", "data/panel.db")))

    def log_path(self) -> str:
        return self.resolve(str(self.get("log_file", "")))

    def pricing_path(self) -> str:
        return self.resolve(str(self.get("pricing_file", "pricing.json")))

    # ---------------------------------------------------------------- 输出
    def public_dict(self, redact: bool = True) -> Dict[str, Any]:
        """给 Web UI 的配置视图；redact=True 时抹掉敏感值（保留「是否已设置」）。"""
        import copy
        data = copy.deepcopy(self.data)
        if redact:
            admin = data.get("admin") or {}
            if admin.get("password_hash"):
                admin["password_hash"] = "********"
            notify = data.get("notify") or {}
            for key in ("telegram_bot_token", "webhook_url"):
                if notify.get(key):
                    notify[key] = _mask(notify[key])
            for node in data.get("nodes") or []:
                if node.get("management_key"):
                    node["management_key"] = _mask(node["management_key"])
        return data

    def node_bootstraps(self) -> List[Dict[str, Any]]:
        out = []
        for item in self.get("nodes") or []:
            if not isinstance(item, dict) or not item.get("base_url"):
                continue
            out.append({
                "name": item.get("name") or "default",
                "base_url": str(item["base_url"]).rstrip("/"),
                "management_key": item.get("management_key") or "",
                "api_prefix": item.get("api_prefix") or "auto",
            })
        return out


def _mask(value: str) -> str:
    s = str(value)
    if len(s) <= 10:
        return "*" * len(s) if s else ""
    return f"{s[:6]}…{s[-4:]}"
