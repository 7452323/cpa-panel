#!/usr/bin/env python3
"""测试运行器：`python3 tests/run_all.py [模块名...]`。

两个「为什么需要它」的理由，都是踩过的坑：

1. 运行环境可能是**常驻解释器**（同一个进程里反复执行脚本），
   `sys.modules` 会跨次保留 —— 于是「改了代码，跑的还是旧逻辑」。
   所以这里先清掉 `cpapanel*` / `tests*` 的模块缓存。
2. 在某些文件系统（粗粒度 mtime、容器快照）上 CPython 可能命中陈旧 `.pyc`，
   表现为 traceback 的行号与源码对不上。所以同时清 `__pycache__`。

退出码：0 全通过；1 有失败/错误。
"""

from __future__ import annotations

import importlib
import io
import os
import shutil
import sys
import time
import unittest
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_last_run.log")

TARGET_PREFIXES = ("cpapanel", "tests")


def purge_bytecode() -> int:
    """删除项目内的所有 __pycache__ 目录。"""
    removed = 0
    for root, dirs, _files in os.walk(ROOT):
        for name in list(dirs):
            if name == "__pycache__":
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
                dirs.remove(name)
                removed += 1
    return removed


def reset_modules() -> int:
    """把本项目相关的模块从 sys.modules 里摘掉，保证下次导入读的是磁盘上的最新代码。"""
    dropped = 0
    for name in list(sys.modules):
        if name in TARGET_PREFIXES or name.split(".")[0] in TARGET_PREFIXES:
            del sys.modules[name]
            dropped += 1
    importlib.invalidate_caches()
    return dropped


def main(argv: list) -> int:
    warnings.simplefilter("ignore", ResourceWarning)
    os.chdir(ROOT)
    purged = purge_bytecode()
    dropped = reset_modules()

    loader = unittest.TestLoader()
    if argv:
        suite = unittest.TestSuite()
        for name in argv:
            suite.addTests(loader.loadTestsFromName(name))
    else:
        suite = loader.discover("tests", top_level_dir=".")

    buf = io.StringIO()
    runner = unittest.TextTestRunner(stream=buf, verbosity=2)
    started = time.time()
    result = runner.run(suite)
    elapsed = time.time() - started

    total = result.testsRun
    failures = result.failures
    errors = result.errors
    skipped = result.skipped
    print(f"运行 {total} 个用例，用时 {elapsed:.1f}s"
          f"（清理 {purged} 个 __pycache__、{dropped} 个模块缓存）")
    print(f"通过 {total - len(failures) - len(errors)} / 失败 {len(failures)} / "
          f"错误 {len(errors)} / 跳过 {len(skipped)}")

    for label, items in (("失败", failures), ("错误", errors)):
        for test, traceback in items:
            print(f"\n[{label}] {test}")
            lines = [ln for ln in traceback.strip().splitlines() if ln.strip()]
            for line in lines[-14:]:
                print("   " + line)

    with open(LOG_PATH, "w", encoding="utf-8") as fh:
        fh.write(buf.getvalue())
    print(f"\n完整输出：{LOG_PATH}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
