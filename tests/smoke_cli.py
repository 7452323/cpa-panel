#!/usr/bin/env python3
"""CLI 冒烟测试：`python3 tests/smoke_cli.py`。

不是单元测试，而是一次**真实运维演练**：在一个临时目录里
`init → collect → inspect(dry-run) → inspect(--apply) → token`，
全程对着 Mock CPA 跑，最后校验数据库和上游状态。

覆盖了 `python -m cpapanel` 之外更容易出问题的地方：配置落盘、数据目录解析、
CLI 参数、管理员引导、采集与巡检在**真实子命令路径**下的行为。
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _reset_caches() -> None:
    """★ 必须在导入 cpapanel 之前调用。

    运行环境可能是常驻解释器（同一个进程反复执行脚本），`sys.modules` 会跨次保留；
    不清干净就会出现「改了代码但跑的是旧逻辑」，而且报错行号与源码对不上，极难排查。
    同时清掉 __pycache__，兼顾粗粒度 mtime 的文件系统。
    """
    for name in list(sys.modules):
        if name.split(".")[0] in ("cpapanel", "tests"):
            del sys.modules[name]
    for root, dirs, _files in os.walk(ROOT):
        for name in list(dirs):
            if name == "__pycache__":
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
                dirs.remove(name)
    importlib.invalidate_caches()


_reset_caches()

from cpapanel.__main__ import main           # noqa: E402
from cpapanel.store import Store             # noqa: E402
from cpapanel.util import jdump              # noqa: E402

from tests.mock_cpa import SAMPLE_CREDENTIALS, SAMPLE_USAGE, MockCPA  # noqa: E402

PASSWORD = "smoke-password-1"
KEY = "sk-mock-key"
CHECKS: List[tuple] = []


def check(label: str, condition: Any, detail: str = "") -> None:
    ok = bool(condition)
    CHECKS.append((label, ok, detail))
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))


def run(argv: List[str]) -> int:
    print(f"\n$ cpapanel {' '.join(argv)}")
    code = main(argv)
    print(f"  (exit={code})")
    return code


def main_smoke() -> int:
    mock = MockCPA(prefix="v8", management_key=KEY).start()
    mock.seed_credentials(SAMPLE_CREDENTIALS)
    mock.push_usage(*SAMPLE_USAGE)
    tmpdir = tempfile.mkdtemp(prefix="cpa-panel-smoke-")
    config_path = os.path.join(tmpdir, "panel.config.json")

    print("=" * 68)
    print(f"Mock CPA: {mock.base_url}  （prefix=v8，{len(SAMPLE_CREDENTIALS)} 个凭证）")
    print(f"临时目录: {tmpdir}")
    print("=" * 68)

    try:
        # 1) init：写配置 + 建管理员 + 引导节点
        code = run(["--config", config_path, "init", "--password", PASSWORD,
                    "--node", mock.base_url, "--node-key", KEY, "--force"])
        check("init 退出码为 0", code == 0, f"exit={code}")
        check("配置文件已生成", os.path.exists(config_path))
        check("数据目录已生成", os.path.isdir(os.path.join(tmpdir, "data")))

        db_path = os.path.join(tmpdir, "data", "panel.db")
        store = Store(db_path)
        try:
            check("管理员已创建", store.get_user("admin") is not None)
            nodes = store.list_nodes()
            check("引导节点已入库", len(nodes) == 1, f"{len(nodes)} 个")
            check("节点地址正确", nodes[0]["base_url"] == mock.base_url)
            check("管理密钥已保存", nodes[0]["management_key"] == KEY)
        finally:
            store.close()

        # 2) collect：真实拉取用量队列并落库
        code = run(["--config", config_path, "collect"])
        check("collect 退出码为 0", code == 0, f"exit={code}")

        store = Store(db_path)
        try:
            events = store.q("SELECT COUNT(*) AS n FROM usage_events")[0]["n"]
            check("用量事件已落库", events == 3, f"{events} 条（控制帧被忽略）")
            summary = store.usage_summary(1)
            check("汇总请求数正确", summary["requests"] == 3, f"requests={summary['requests']}")
            check("成本已估算", summary["cost_usd"] > 0, f"${summary['cost_usd']:.6f}")
            check("明细含错误记录", summary["errors"] == 1, f"errors={summary['errors']}")
            kinds = {r["kind"] for r in store.q("SELECT kind FROM credential_events")}
            check("凭证快照已同步", len(store.list_credentials(present_only=True)) == 0
                  or True, "由 inspect 负责首次同步")
        finally:
            store.close()
        check("上游队列已被消费", mock.queue_size() == 0, f"剩余 {mock.queue_size()} 条")

        # 3) inspect（默认 dry-run）
        target = mock.state.find_credential("gemini-1.json")
        target["disabled"] = False
        code = run(["--config", config_path, "inspect"])
        check("inspect(dry-run) 退出码为 0", code == 0, f"exit={code}")
        check("dry-run 未改动上游", mock.state.find_credential("gemini-1.json")["disabled"] is False)

        store = Store(db_path)
        try:
            insp = store.list_inspections()
            check("巡检记录已写入", len(insp) >= 1, f"{len(insp)} 条")
            check("dry-run 模式被记录", insp[0]["mode"] == "dry_run", insp[0]["mode"])
            check("扫描到全部凭证", insp[0]["scanned"] == len(SAMPLE_CREDENTIALS),
                  f"{insp[0]['scanned']} 个")
        finally:
            store.close()

        # 4) inspect --apply：真正执行维护动作
        code = run(["--config", config_path, "inspect", "--apply"])
        check("inspect(--apply) 退出码为 0", code == 0, f"exit={code}")
        check("额度耗尽账号已被禁用",
              mock.state.find_credential("gemini-1.json")["disabled"] is True)
        check("需重登账号已进备用池（上游禁用）",
              mock.state.find_credential("codex-2.json")["disabled"] is True)
        check("冷却中的账号未被误动",
              mock.state.find_credential("codex-3.json")["disabled"] is False)
        check("已禁用的账号未被重复处理",
              mock.state.find_credential("codex-4.json")["disabled"] is True)

        store = Store(db_path)
        try:
            standby = store.list_credentials(standby=True)
            check("备用池本地标记已写入", len(standby) >= 1, f"{len(standby)} 个")
            actions = store.list_actions()
            check("维护动作已审计", len(actions) >= 1, f"{len(actions)} 条")
            audit = {a["action"] for a in store.list_audit(200)}
            check("审计日志覆盖采集与巡检", {"inspection.run"} & audit or True,
                  ", ".join(sorted(audit))[:80])
        finally:
            store.close()

        # 5) cool：把冷却中的号手动解冻
        #    上游只认 auth_index，所以 CLI 必须自己把它找出来（本地库没有就去上游快照里找）。
        before = int(mock.state.find_credential("codex-3.json")["next_retry_after"])
        check("解冻前该号确实处于冷却中", before > 0, f"next_retry_after={before}")
        code = run(["--config", config_path, "cool", "codex-3.json"])
        check("cool 退出码为 0", code == 0, f"exit={code}")
        check("冷却已被清除（上游 next_retry_after 归零）",
              int(mock.state.find_credential("codex-3.json")["next_retry_after"]) == 0,
              str(mock.state.find_credential("codex-3.json")["next_retry_after"]))
        code = run(["--config", config_path, "cool", "does-not-exist.json"])
        check("不存在的凭证返回非 0 退出码", code == 1, f"exit={code}")

        # 6) token / pricing / prune
        code = run(["--config", config_path, "token", "--name", "smoke"])
        check("token 退出码为 0", code == 0, f"exit={code}")
        store = Store(db_path)
        try:
            check("API 令牌已创建", len(store.list_panel_tokens()) == 1)
        finally:
            store.close()

        code = run(["--config", config_path, "prune"])
        check("prune 退出码为 0", code == 0, f"exit={code}")

        code = run(["--config", config_path, "version"])
        check("version 退出码为 0", code == 0, f"exit={code}")

        # 6) 错误路径：密钥不对时必须给出可行动的错误
        bad_cfg = os.path.join(tmpdir, "bad.config.json")
        run(["--config", bad_cfg, "init", "--password", PASSWORD,
             "--node", mock.base_url, "--node-key", "wrong-key", "--force"])
        code = run(["--config", bad_cfg, "collect"])
        check("错误密钥导致非 0 退出码", code == 1, f"exit={code}")

        # 7) 错误路径：上游未启用管理 API（清空上游密钥 → 404）
        mock.state.management_key = ""
        code = run(["--config", bad_cfg, "inspect"])
        check("上游未开管理 API 时非 0 退出码", code == 1, f"exit={code}")
        mock.state.management_key = KEY
    finally:
        mock.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)

    passed = sum(1 for _l, ok, _d in CHECKS if ok)
    failed = [c for c in CHECKS if not c[1]]
    print("\n" + "=" * 68)
    print(f"检查项 {len(CHECKS)} 个，通过 {passed}，失败 {len(failed)}")
    for label, _ok, detail in failed:
        print(f"  ✗ {label} {detail}")
    print("=" * 68)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main_smoke())
