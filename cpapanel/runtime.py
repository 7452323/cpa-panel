"""运行时装配：把配置、存储、采集器、巡检器、通知与 Web 应用拼起来。

CLI 与集成测试都用这一份装配逻辑，避免「命令行能跑、测试里跑的是另一套」。
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from .collector import UsageCollector
from .config import Config
from .inspector import AccountInspector
from .log import get, setup
from .notify import Notifier
from .pricing import Pricing
from .security import hash_password
from .store import Store
from .util import ensure_dir
from .web.server import PanelApp

log = get("cpapanel.runtime")


class Panel:
    """一次完整的运行时装配结果。"""

    def __init__(self, config: Config, store: Store, pricing: Pricing, notifier: Notifier,
                 collector: UsageCollector, inspector: AccountInspector, app: PanelApp):
        self.config = config
        self.store = store
        self.pricing = pricing
        self.notifier = notifier
        self.collector = collector
        self.inspector = inspector
        self.app = app

    def start_background(self) -> None:
        self.collector.start()
        self.inspector.start()

    def stop_background(self) -> None:
        self.collector.stop()
        self.inspector.stop()

    def close(self) -> None:
        self.stop_background()
        self.store.close()


def build(config: Config, setup_logging: bool = True) -> Panel:
    if setup_logging:
        setup(level=str(config.get("log_level") or "INFO"), log_file=config.log_path() or None)

    data_dir = config.data_dir()
    ensure_dir(data_dir)

    store = Store(config.database_path())
    pricing = Pricing.load(config.pricing_path())
    notifier = Notifier(store, config)
    collector = UsageCollector(store, config, pricing, notifier)
    inspector = AccountInspector(store, config, notifier)
    app = PanelApp(config, store, pricing, collector, inspector, notifier)

    # 引导节点：配置文件/环境变量里给的节点，首次启动时导入
    created = store.ensure_bootstrap_nodes(config.node_bootstraps())
    if created:
        log.info("已从配置引导 %d 个节点", len(created))

    store.set_meta("panel_version", __import__("cpapanel").__version__)
    return Panel(config, store, pricing, notifier, collector, inspector, app)


def ensure_admin(store: Store, config: Config, username: Optional[str] = None,
                 password: Optional[str] = None) -> Dict[str, Any]:
    """确保管理员存在。返回 {created, username, password}（password 仅在新建时返回）。"""
    username = (username or config.get("admin.username") or "admin").strip() or "admin"
    created = False
    if password is None:
        password = config.bootstrap_admin_password()
    existing = store.get_user(username)
    if existing is None:
        if not password:
            raise ValueError(
                "尚未创建管理员：请用 `--password` 指定，或设置环境变量 CPAPANEL_ADMIN_PASSWORD")
        store.create_user(username, hash_password(password), "admin")
        created = True
        log.info("已创建管理员 %s", username)
    elif password:
        store.set_user_password(username, hash_password(password))
        log.info("已更新管理员 %s 的口令", username)
    if created:
        config.set("admin.username", username)
    return {"created": created, "username": username, "password": password if created else None}


def describe_paths(config: Config) -> Dict[str, str]:
    return {
        "config": config.path or "(未指定)",
        "data_dir": config.data_dir(),
        "database": config.database_path(),
        "log": config.log_path(),
        "pricing": config.pricing_path(),
        "static": os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "static"),
    }
