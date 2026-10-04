"""账号巡检与自动化维护。

它解决的是号池运维最痛的问题：**哪些号该扔、哪些号在冷却、池子还够不够用**。

安全设计（比功能更重要）：
* 默认 `dry_run=true`：只产出计划 + 落库，不碰上游；
* 删除动作三重开关：`delete_*` 开关 + `max_deletes_per_run` 上限 + 跳过 `runtime_only` 凭证；
* 所有动作都写 `actions` 表与审计日志，可追溯；
* 单个动作失败不影响其余动作。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from .cpa import CPAClient, CPAError
from .log import get, note
from .models import (STATE_COOLING, STATE_DISABLED, STATE_HEALTHY, STATE_QUOTA_EXHAUSTED,
                     STATE_UNAUTHORIZED, STATE_UNKNOWN, classify_credential, normalize_auth_file)
from .util import as_bool, jdump, now_ts, short

log = get("cpapanel.inspector")

ACTION_DISABLE = "disable"
ACTION_ENABLE = "enable"
ACTION_DELETE = "delete"
ACTION_STANDBY = "standby"
ACTION_PROMOTE = "promote"
ACTION_MARK = "mark"


class AccountInspector:
    def __init__(self, store: Any, config: Any, notifier: Any):
        self.store = store
        self.config = config
        self.notifier = notifier
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self.last_result: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ 调度

    def start(self) -> None:
        if not self.config.get("inspector.enabled", True):
            log.info("巡检未启用（inspector.enabled=false）")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="account-inspector", daemon=True)
        self._thread.start()
        log.info("巡检已启动：间隔 %ss，模式 %s",
                 self.config.get("inspector.interval_seconds"),
                 "dry-run" if self.config.get("inspector.dry_run", True) else "apply")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def ensure_running(self) -> None:
        self.stop()
        self.start()

    def _loop(self) -> None:
        interval = max(60, int(self.config.get("inspector.interval_seconds") or 900))
        # 启动后先等一个间隔，避免和面板启动抢资源
        if self._stop.wait(min(30, interval)):
            return
        while not self._stop.is_set():
            try:
                if self.store.get_setting("inspector.auto", True):
                    self.run(reason="scheduled")
            except Exception as exc:  # noqa: BLE001
                log.exception("巡检轮次异常：%s", exc)
            self._stop.wait(interval)

    # ------------------------------------------------------------------ 主流程

    def run(self, node_id: Optional[int] = None, dry_run: Optional[bool] = None,
            reason: str = "manual") -> Dict[str, Any]:
        if dry_run is None:
            dry_run = bool(self.config.get("inspector.dry_run", True))
        nodes = [self.store.get_node(node_id)] if node_id else list(self.store.list_nodes(only_enabled=True))
        results = []
        for node in nodes:
            if not node:
                continue
            try:
                results.append(self.inspect_node(node, dry_run=dry_run, reason=reason))
            except Exception as exc:  # noqa: BLE001
                log.exception("节点 %s 巡检失败", node["name"])
                results.append({"node_id": node["id"], "node_name": node["name"],
                                "ok": False, "error": str(exc)})
        summary = {"ts": now_ts(), "reason": reason, "dry_run": dry_run, "nodes": results}
        with self._lock:
            self.last_result = summary
        return summary

    # ------------------------------------------------------------------ 单节点

    def inspect_node(self, node: Any, dry_run: bool = True, reason: str = "manual") -> Dict[str, Any]:
        node_id = int(node["id"])
        client = CPAClient(
            base_url=node["base_url"], management_key=node["management_key"],
            prefix=node["api_prefix"] if node["api_prefix"] in ("v0", "v8") else "auto",
            timeout=(float(self.config.get("http.connect_timeout") or 5),
                     float(self.config.get("http.read_timeout") or 20)),
            verify_tls=bool(self.config.get("http.verify_tls", True)),
        )
        inspection_id = self.store.start_inspection(node_id, "dry_run" if dry_run else "apply")
        mode = self.config.get("inspector.dry_run", True) and dry_run

        try:
            raw = client.list_auth_files()
            entries = raw.get("files") or []
        except CPAError as exc:
            self.store.finish_inspection(inspection_id, error=short(str(exc), 500), scanned=0)
            self.store.audit("inspection.error", actor="inspector", target=str(node["name"]),
                             detail=short(str(exc), 300))
            self.notifier.notify("inspection.error", f"巡检失败：{node['name']}", str(exc))
            return {"node_id": node_id, "node_name": node["name"], "ok": False, "error": str(exc),
                    "inspection_id": inspection_id}

        # 1) 落库快照（并把「新增/消失/状态变化」变成事件）
        normalized = [normalize_auth_file(item) for item in entries if isinstance(item, dict)]
        sync = self.store.sync_credentials(node_id, normalized)

        # 2) 逐个分类 + 采样
        ts = now_ts()
        counts = {STATE_HEALTHY: 0, STATE_COOLING: 0, STATE_QUOTA_EXHAUSTED: 0,
                  STATE_UNAUTHORIZED: 0, STATE_DISABLED: 0, STATE_UNKNOWN: 0}
        plan: List[Dict[str, Any]] = []
        for item in normalized:
            classified = item.get("_classified") or classify_credential(item)
            state = classified.get("state") or STATE_UNKNOWN
            counts[state] = counts.get(state, 0) + 1
            row = self.store.q1(
                "SELECT * FROM credentials WHERE node_id = ? AND name = ? AND auth_index = ?",
                (node_id, item.get("name"), item.get("auth_index") or ""))
            if row is not None:
                self.store.add_credential_sample(
                    int(row["id"]), node_id, ts, state, bool(item.get("disabled")),
                    bool(item.get("unavailable")), int(item.get("success") or 0),
                    int(item.get("failed") or 0), item.get("next_retry_after"),
                    classified.get("reason") or "")
            actions = self._plan_for(item, classified, row)
            for action in actions:
                action["_row"] = row
                action["_classified"] = classified
                plan.append(action)

        # 3) 池子水位与备用池
        pool_actions, pool_info = self._plan_pool(node_id)
        plan.extend(pool_actions)

        # 4) 执行（dry-run 时只记录计划）
        executed: List[Dict[str, Any]] = []
        failures = 0
        delete_budget = int(self.config.get("inspector.max_deletes_per_run") or 20)
        for action in plan:
            if not dry_run and action["action"] == ACTION_DELETE and delete_budget <= 0:
                executed.append(dict(action, result="skipped", detail="已达单轮删除上限"))
                continue
            result, detail = "planned", ""
            if not dry_run:
                try:
                    result, detail = self._execute(client, action)
                except CPAError as exc:
                    result, detail = "failed", short(str(exc), 300)
                    failures += 1
                if result == "ok" and action["action"] == ACTION_DELETE:
                    delete_budget -= 1
            self.store.add_action(
                inspection_id, node_id, action.get("name") or "", action["action"],
                action.get("reason") or "", result, detail or action.get("detail") or "")
            executed.append(dict(action, result=result, result_detail=detail))
            # dry-run 不改变任何状态（包括本地元数据），否则「只做计划」就变成假话
            if not dry_run and result == "ok":
                self._apply_local_state(node_id, action, dry_run=False)

        summary = {
            "counts": counts,
            "changes": {k: v for k, v in sync.items() if k != "changes"},
            "changes_detail": sync.get("changes") or [],
            "pool": pool_info,
            "quality": _quality(entries),
        }
        self.store.finish_inspection(
            inspection_id, scanned=len(normalized), active=counts.get(STATE_HEALTHY, 0),
            unauthorized=counts.get(STATE_UNAUTHORIZED, 0),
            quota_exhausted=counts.get(STATE_QUOTA_EXHAUSTED, 0),
            cooling=counts.get(STATE_COOLING, 0), disabled=counts.get(STATE_DISABLED, 0),
            standby=pool_info.get("standby", 0), planned=len(plan), executed=sum(
                1 for a in executed if a.get("result") == "ok"),
            failures=failures, summary_json=summary)

        result = {
            "node_id": node_id, "node_name": node["name"], "ok": True,
            "inspection_id": inspection_id, "dry_run": dry_run, "reason": reason,
            "scanned": len(normalized),
            "counts": counts, "changes": summary["changes"], "pool": pool_info,
            "planned": len(plan), "executed": sum(1 for a in executed if a.get("result") == "ok"),
            "failures": failures, "plan": plan, "actions": executed,
            "executed_detail": {a["action"]: a.get("name") for a in executed
                                if a.get("result") == "ok"},
        }
        with self._lock:
            self.last_result = result

        # 5) 告警
        if counts.get(STATE_UNAUTHORIZED, 0) > 0:
            self.notifier.notify(
                "inspection.unauthorized",
                f"{node['name']} 有账号需要重新登录",
                f"发现 {counts.get(STATE_UNAUTHORIZED, 0)} 个失效凭证，"
                f"本轮{'（dry-run 仅计划）' if dry_run else ''}执行 {result['executed']} 项维护动作。")
        if pool_info.get("below_target"):
            self.notifier.notify("inspection.low_pool", f"{node['name']} 可用账号低于目标",
                                 f"当前可用 {pool_info.get('active', 0)}，目标 {pool_info.get('target')}，"
                                 f"备用池 {pool_info.get('standby', 0)}。")
        note("info", "巡检完成", node=node["name"], mode="dry-run" if dry_run else "apply",
             scanned=len(normalized), planned=len(plan), executed=result["executed"])
        return result

    # ------------------------------------------------------------------ 计划

    def _plan_for(self, item: Dict[str, Any], classified: Dict[str, Any],
                  row: Any) -> List[Dict[str, Any]]:
        name = item.get("name") or ""
        state = classified.get("state")
        runtime_only = as_bool(item.get("runtime_only"))
        planned: List[Dict[str, Any]] = []

        if runtime_only:
            # 插件虚拟凭证不能直接改/删（上游明确拒绝）
            return planned

        if state == STATE_UNAUTHORIZED:
            if self.config.get("inspector.delete_unauthorized", False):
                planned.append({"action": ACTION_DELETE, "name": name,
                                "reason": classified.get("reason") or "认证失效"})
            elif self.config.get("inspector.disable_unauthorized", False):
                planned.append({"action": ACTION_DISABLE, "name": name,
                                "reason": classified.get("reason") or "认证失效"})
            elif self.config.get("inspector.standby_pool", True):
                planned.append({"action": ACTION_STANDBY, "name": name,
                                "reason": classified.get("reason") or "认证失效（移入备用池）"})
            else:
                planned.append({"action": ACTION_MARK, "name": name,
                                "reason": classified.get("reason") or "认证失效"})
        elif state == STATE_QUOTA_EXHAUSTED:
            if self.config.get("inspector.delete_quota_exhausted", False):
                planned.append({"action": ACTION_DELETE, "name": name,
                                "reason": "额度耗尽（按配置删除）"})
            elif self.config.get("inspector.disable_quota_exhausted", True):
                if not as_bool(item.get("disabled")):
                    planned.append({"action": ACTION_DISABLE, "name": name,
                                    "reason": "额度耗尽（按配置禁用，等待窗口恢复后手动/自动启用）"})
        elif state == STATE_HEALTHY and row is not None and int(row["standby"]) == 1:
            # 已恢复健康的备用号：回到活跃
            planned.append({"action": ACTION_PROMOTE, "name": name, "reason": "已恢复健康，移出备用池"})
        return planned

    def _plan_pool(self, node_id: int) -> tuple:
        target = int(self.config.get("inspector.target_active") or 0)
        counts = self.store.credential_counts(node_id)
        standby_rows = self.store.list_credentials(node_id=node_id, standby=True, present_only=True)
        pool = {"active": counts.get("active", 0), "standby": len(standby_rows),
                "target": target, "below_target": False}
        planned: List[Dict[str, Any]] = []
        if target > 0 and counts.get("active", 0) < target:
            pool["below_target"] = True
            if self.config.get("inspector.promote_standby_when_low", True):
                deficit = target - counts.get("active", 0)
                for row in standby_rows[:deficit]:
                    planned.append({
                        "action": ACTION_PROMOTE, "name": row["name"],
                        "reason": f"可用数不足（{counts.get('active', 0)}/{target}），从备用池补位",
                        "_row": row,
                    })
                    pool["standby"] = max(0, pool["standby"] - 1)
        return planned, pool

    # ------------------------------------------------------------------ 执行

    def _execute(self, client: CPAClient, action: Dict[str, Any]) -> tuple:
        name = action.get("name")
        if not name:
            return "skipped", "缺少凭证名"
        try:
            if action["action"] == ACTION_DISABLE:
                client.patch_auth_file_status(name, disabled=True)
                return "ok", "已禁用"
            if action["action"] == ACTION_ENABLE:
                client.patch_auth_file_status(name, disabled=False)
                return "ok", "已启用"
            if action["action"] == ACTION_DELETE:
                client.delete_auth_file(name)
                return "ok", "已删除"
            if action["action"] == ACTION_PROMOTE:
                client.patch_auth_file_status(name, disabled=False)
                return "ok", "已启用（移出备用池）"
            if action["action"] == ACTION_STANDBY:
                # 备用池是**本地**概念：上游没有这个状态。
                # 语义 = 「不再参与调度，但保留凭证以便将来恢复」→ 上游动作就是禁用。
                client.patch_auth_file_status(name, disabled=True)
                return "ok", "已移入备用池（上游禁用 + 本地标记）"
        except CPAError as exc:
            if exc.management_unavailable:
                raise
            return "failed", short(str(exc), 300)
        return "skipped", "无需动作"

    def _apply_local_state(self, node_id: int, action: Dict[str, Any], dry_run: bool) -> None:
        row = action.get("_row")
        if row is None:
            row = self.store.q1("SELECT * FROM credentials WHERE node_id = ? AND name = ?",
                                (node_id, action.get("name")))
        if row is None:
            # 计划里没有数据库行（例如刚 promote 的项），按名字补查
            return
        cred_id = int(row["id"])
        if action["action"] == ACTION_STANDBY:
            self.store.set_credential_standby(cred_id, True)
        elif action["action"] in (ACTION_PROMOTE, ACTION_ENABLE):
            self.store.set_credential_standby(cred_id, False)
            self.store.ex("UPDATE credentials SET disabled = 0 WHERE id = ?", (cred_id,))
        elif action["action"] == ACTION_DISABLE:
            self.store.ex("UPDATE credentials SET disabled = 1 WHERE id = ?", (cred_id,))
        elif action["action"] == ACTION_DELETE:
            self.store.mark_credential_deleted(cred_id)

    # ------------------------------------------------------------------ 手动动作

    def disable_credential(self, node_id: int, name: str) -> Dict[str, Any]:
        return self._manual(node_id, name, ACTION_DISABLE)

    def enable_credential(self, node_id: int, name: str) -> Dict[str, Any]:
        return self._manual(node_id, name, ACTION_ENABLE)

    def delete_credential(self, node_id: int, name: str) -> Dict[str, Any]:
        return self._manual(node_id, name, ACTION_DELETE)

    def refresh_credential(self, node_id: int, name: str) -> Dict[str, Any]:
        node = self.store.get_node(node_id)
        if not node:
            return {"ok": False, "error": "节点不存在"}
        client = self._client(node)
        try:
            result = client.refresh_auth_files(name=name)
        except CPAError as exc:
            return {"ok": False, "error": str(exc), "status": exc.status}
        self.store.add_action(None, node_id, name, "refresh", "手动刷新", "ok", "")
        self.store.audit("credential.refresh", actor="user", target=name)
        return {"ok": True, "result": result}

    def _manual(self, node_id: int, name: str, action: str) -> Dict[str, Any]:
        node = self.store.get_node(node_id)
        if not node:
            return {"ok": False, "error": "节点不存在"}
        client = self._client(node)
        try:
            result, detail = self._execute(client, {"action": action, "name": name})
        except CPAError as exc:
            self.store.add_action(None, node_id, name, action, "手动操作", "failed", str(exc))
            return {"ok": False, "error": str(exc), "status": exc.status}
        row = self.store.q1("SELECT * FROM credentials WHERE node_id = ? AND name = ?",
                            (node_id, name))
        if row is not None:
            self._apply_local_state(node_id, {"action": action, "_row": row}, dry_run=False)
        self.store.add_action(None, node_id, name, action, "手动操作", result, detail)
        self.store.audit(f"credential.{action}", actor="user", target=name, detail=detail)
        return {"ok": result == "ok", "result": result, "detail": detail}

    def _client(self, node: Any) -> CPAClient:
        return CPAClient(
            base_url=node["base_url"], management_key=node["management_key"],
            prefix=node["api_prefix"] if node["api_prefix"] in ("v0", "v8") else "auto",
            timeout=(float(self.config.get("http.connect_timeout") or 5),
                     float(self.config.get("http.read_timeout") or 20)),
            verify_tls=bool(self.config.get("http.verify_tls", True)))

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self.last_result or {}


def _quality(entries: List[Any]) -> Dict[str, Any]:
    """快照质量：有多少条目拿不到关键字段（用于暴露上游兼容性问题）。"""
    total = len(entries)
    missing_provider = sum(1 for e in entries if isinstance(e, dict)
                           and not (e.get("provider") or e.get("type")))
    missing_index = sum(1 for e in entries if isinstance(e, dict) and not e.get("auth_index"))
    return {"total": total, "missing_provider": missing_provider, "missing_auth_index": missing_index}
