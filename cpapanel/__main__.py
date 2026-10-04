"""命令行入口：`python -m cpapanel <命令>`。

命令一览：
  serve      启动面板（Web + 采集 + 巡检）
  init       生成配置、创建管理员、可选写入引导节点
  collect    立即采集一次（排障用，可 --once）
  inspect    立即巡检一次（默认 dry-run，--apply 才真正改上游）
  password   修改管理员口令
  token      创建面板 API 令牌（给脚本/CI 用）
  pricing    导出价格覆盖模板
  prune      清理过期数据
  version    打印版本
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from . import APP_NAME, __version__
from .config import DEFAULT_CONFIG, Config
from .log import get, setup
from .pricing import write_template
from .runtime import build, ensure_admin
from .security import new_token, token_hash
from .util import jdump, jdump_pretty, now_ts

log = get("cpapanel.cli")

DEFAULT_CONFIG_PATH = os.environ.get("CPAPANEL_CONFIG") or "panel.config.json"


# --------------------------------------------------------------------------- 参数


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cpapanel",
        description=f"{APP_NAME} {__version__} —— 自托管的 CLIProxyAPI(CPA) 账号管理面板",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python -m cpapanel init --node http://127.0.0.1:8317 --node-key sk-xxx\n"
               "  python -m cpapanel serve --host 0.0.0.0 --port 18317\n")
    parser.add_argument("--config", "-c", default=DEFAULT_CONFIG_PATH,
                        help=f"配置文件路径（默认 {DEFAULT_CONFIG_PATH}）")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="启动面板")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--no-collector", action="store_true", help="不启动采集线程")
    p_serve.add_argument("--no-inspector", action="store_true", help="不启动巡检线程")

    p_init = sub.add_parser("init", help="初始化配置与管理员")
    p_init.add_argument("--password", default=None, help="管理员口令（不填则读环境变量）")
    p_init.add_argument("--username", default=None)
    p_init.add_argument("--node", default=None, help="引导 CPA 节点地址，如 http://127.0.0.1:8317")
    p_init.add_argument("--node-key", default=None, help="该节点的管理密钥")
    p_init.add_argument("--node-name", default="default")
    p_init.add_argument("--node-prefix", default="auto", choices=["auto", "v0", "v8"])
    p_init.add_argument("--force", action="store_true", help="覆盖已存在的配置文件")

    p_collect = sub.add_parser("collect", help="立即采集一次用量")
    p_collect.add_argument("--node-id", type=int, default=None)

    p_inspect = sub.add_parser("inspect", help="立即巡检一次")
    p_inspect.add_argument("--node-id", type=int, default=None)
    p_inspect.add_argument("--apply", action="store_true", help="真正执行动作（默认 dry-run）")
    p_inspect.add_argument("--json", action="store_true", help="输出完整 JSON")

    p_pwd = sub.add_parser("password", help="修改管理员口令")
    p_pwd.add_argument("--username", default=None)
    p_pwd.add_argument("--password", default=None)

    p_token = sub.add_parser("token", help="创建面板 API 令牌")
    p_token.add_argument("--name", default="cli")
    p_token.add_argument("--role", default="admin", choices=["admin", "viewer"])

    p_pricing = sub.add_parser("pricing", help="导出价格覆盖模板")
    p_pricing.add_argument("--out", default="pricing.json")

    p_prune = sub.add_parser("prune", help="清理过期数据")
    p_prune.add_argument("--usage-days", type=int, default=180)
    p_prune.add_argument("--sample-days", type=int, default=30)
    p_prune.add_argument("--audit-days", type=int, default=90)

    sub.add_parser("version", help="打印版本")
    return parser


# --------------------------------------------------------------------------- 命令


def cmd_init(args: argparse.Namespace) -> int:
    path = args.config
    if os.path.exists(path) and not args.force:
        log.info("配置文件已存在：%s（用 --force 覆盖）", path)
        config = Config.load(path)
    else:
        config = Config()
        config.path = path
        config.set("host", config.get("host"))
        config.save(path)
        log.info("已生成配置文件：%s", path)
        print("配置文件已生成：", os.path.abspath(path))

    if args.node:
        poll = {"name": args.node_name, "base_url": args.node.rstrip("/"),
                "management_key": args.node_key or "", "api_prefix": args.node_prefix}
        nodes = [n for n in (config.get("nodes") or []) if n.get("base_url") != poll["base_url"]]
        nodes.append(poll)
        config.set("nodes", nodes)
        config.save()
        print(f"已写入引导节点：{poll['base_url']}（前缀 {poll['api_prefix']}）")

    panel = build(config)
    try:
        info = ensure_admin(panel.store, config, username=args.username, password=args.password)
        if info["created"]:
            print("=" * 56)
            print(f"  管理员已创建：{info['username']}")
            print(f"  初始口令：{info['password']}")
            print("  请立即登录后修改口令。")
            print("=" * 56)
        else:
            print(f"管理员已存在：{info['username']}（口令未变更）")
        print(f"数据库：{panel.store.path}")
    finally:
        panel.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    if args.host:
        config.set("host", args.host)
    if args.port:
        config.set("port", int(args.port))
    panel = build(config)
    try:
        ensure_admin(panel.store, config)
        if not args.no_collector and config.get("collector.enabled", True):
            panel.collector.start()
        if not args.no_inspector and config.get("inspector.enabled", True):
            panel.inspector.start()
        host = config.get("host") or "127.0.0.1"
        port = int(config.get("port") or 18317)
        panel.app.notifier.notify("panel.start", "cpa-panel 已启动",
                                  f"监听 {host}:{port}，版本 {__version__}")
        from .web.server import run_forever
        run_forever(panel.app, host, port)
    finally:
        panel.close()
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    """采集一次。**任何节点失败就返回非 0** —— 否则 cron/监控无法发现上游挂了。"""
    panel = build(Config.load(args.config))
    try:
        result = panel.collector.collect_once(args.node_id)
        print(jdump_pretty(result))
        nodes = result.get("nodes") or [result]
        failures = [n for n in nodes if not n.get("ok")]
        for item in failures:
            print(f"错误：节点 {item.get('node_name') or item.get('node_id')} 采集失败："
                  f"{item.get('error')}", file=sys.stderr)
        return 1 if failures else 0
    finally:
        panel.close()


def cmd_inspect(args: argparse.Namespace) -> int:
    panel = build(Config.load(args.config))
    try:
        result = panel.inspector.run(node_id=args.node_id, dry_run=not args.apply, reason="cli")
        failed_nodes = []
        if args.json:
            print(jdump_pretty(result))
            failed_nodes = [n for n in (result.get("nodes") or []) if not n.get("ok")]
        else:
            for node in result.get("nodes") or []:
                if not node.get("ok"):
                    failed_nodes.append(node)
                    print(f"[失败] {node.get('node_name')}: {node.get('error')}", file=sys.stderr)
                    continue
                counts = node.get("counts") or {}
                print(f"[{node['node_name']}] 模式={'apply' if args.apply else 'dry-run'} "
                      f"扫描={node.get('scanned', 0)} 健康={counts.get('healthy', 0)} "
                      f"冷却={counts.get('cooling', 0)} 额度={counts.get('quota_exhausted', 0)} "
                      f"需重登={counts.get('unauthorized', 0)} 已禁用={counts.get('disabled', 0)}")
                for action in node.get("plan") or []:
                    mark = "执行" if args.apply else "计划"
                    print(f"    {mark}: {action.get('action')} {action.get('name')} — {action.get('reason')}")
                if not (node.get("plan") or []):
                    print("    无需动作")
            if not args.apply:
                print("\n提示：这是 dry-run，未改动上游。确认无误后加 --apply 执行。")
        return 1 if failed_nodes else 0
    finally:
        panel.close()


def cmd_password(args: argparse.Namespace) -> int:
    from .security import hash_password
    config = Config.load(args.config)
    panel = build(config)
    try:
        username = args.username or config.get("admin.username") or "admin"
        password = args.password
        if not password:
            import getpass
            password = getpass.getpass("新口令：")
            again = getpass.getpass("再输一次：")
            if password != again:
                print("两次输入不一致", file=sys.stderr)
                return 2
        if len(password) < 8:
            print("口令至少 8 位", file=sys.stderr)
            return 2
        if panel.store.get_user(username) is None:
            panel.store.create_user(username, hash_password(password), "admin")
            print(f"已创建管理员 {username}")
        else:
            panel.store.set_user_password(username, hash_password(password))
            print(f"已更新 {username} 的口令")
    finally:
        panel.close()
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    panel = build(config)
    try:
        raw = new_token()
        panel.store.create_panel_token(args.name, token_hash(raw), args.role)
        print("面板 API 令牌（仅显示这一次）：")
        print(raw)
        print("\n用法：curl -H \"Authorization: Bearer <token>\" http://127.0.0.1:18317/api/overview")
    finally:
        panel.close()
    return 0


def cmd_pricing(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    from .pricing import Pricing
    path = write_template(args.out, Pricing.load(config.pricing_path()))
    print(f"已写入价格模板：{os.path.abspath(path)}")
    print("编辑后把 panel 配置里的 pricing_file 指向它即可生效。")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    panel = build(Config.load(args.config))
    try:
        result = panel.store.prune(args.usage_days, args.sample_days, args.audit_days)
        print(jdump_pretty(result))
    finally:
        panel.close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup(level="INFO")
    if not args.command:
        parser.print_help()
        return 0
    handlers = {
        "init": cmd_init, "serve": cmd_serve, "collect": cmd_collect, "inspect": cmd_inspect,
        "password": cmd_password, "token": cmd_token, "pricing": cmd_pricing, "prune": cmd_prune,
    }
    if args.command == "version":
        print(f"{APP_NAME} {__version__}")
        return 0
    handler = handlers.get(args.command)
    if not handler:
        parser.print_help()
        return 2
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底
        log.error("命令执行失败：%s", exc)
        from .cpa import CPAError
        if isinstance(exc, CPAError):
            print(f"错误：{exc}", file=sys.stderr)
            if exc.management_unavailable:
                print("提示：上游未启用 Management API（通常是没配置管理密钥），"
                      "此时 /v0/management 与 /v8/management 都会返回 404。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
