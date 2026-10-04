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
# 让上游把冷却中的凭证立刻重新放回调度候选（官方：POST /routing/cooldown/reset）
ACTION_COOLDOWN_RESET = "cooldown_reset"


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

        # 3.5) 熔断闸门 —— 就在“已算出要做什么”和“真的去做”之间
        circuit = self._circuit_state(counts, len(normalized))
        allow_execute = (not dry_run) and not circuit["open"]
        if circuit["open"]:
            note("warning", "巡检触发就绪率熔断，本轮只出计划不执行",
                 node=node["name"], **{k: circuit[k] for k in ("ready", "total", "ready_ratio", "threshold")})
            self.store.audit("inspection.circuit_open", actor="inspector",
                             detail=circuit["reason"], ip=None)

        # 4) 执行（dry-run 或熔断时只记录计划）
        executed: List[Dict[str, Any]] = []
        failures = 0
        delete_budget = int(self.config.get("inspector.max_deletes_per_run") or 20)
        for action in plan:
            if allow_execute and action["action"] == ACTION_DELETE and delete_budget <= 0:
                executed.append(dict(action, result="skipped", detail="已达单轮删除上限"))
                continue
            result, detail = "planned", ""
            if allow_execute:
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
            # dry-run / 熔断都不改变任何状态（包括本地元数据），否则「只做计划」就变成假话
            if allow_execute and result == "ok":
                self._apply_local_state(node_id, action, dry_run=False)

        summary = {
            "counts": counts,
            "changes": {k: v for k, v in sync.items() if k != "changes"},
            "changes_detail": sync.get("changes") or [],
            "pool": pool_info,
            "quality": _quality(entries),
            "circuit": circuit,
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
            # mode 要能区分第三种情况：请求了 apply（或后台自动），但被熔断拦住了
            "mode": "dry_run" if dry_run else ("circuit_break" if circuit["open"] else "apply"),
            "circuit": circuit,
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
        if circuit["open"]:
            self.notifier.notify(
                "inspection.circuit_open",
                f"{node['name']} 账号池就绪率过低，已暂停自动维护",
                f"{circuit['reason']}（已计划 {len(plan)} 项动作，本轮一项都没执行）")
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
        note("info", "巡检完成", node=node["name"], mode=result["mode"],
             scanned=len(normalized), planned=len(plan), executed=result["executed"],
             circuit_open=circuit["open"])
        return result

    # ------------------------------------------------------------------ 计划

    # ------------------------------------------------------------------ 熔断

    def _circuit_state(self, counts: Dict[str, int], total: int) -> Dict[str, Any]:
        """就绪率熔断（思路借自 CPA-Codex-Manager 的 emergency_defense）。

        为什么需要它：批量掉号（上游事故、封号潮、IP 被封）时，**巡检自己**往往才是最大的破坏源——
        它会把「集体失效」当成「这些号都该删」，一轮清掉半个池子，而其中很多号改天自己就恢复了。
        所以当就绪率低于阈值时，本轮**直接放弃所有维护动作**，只出计划 + 告警，等人看一眼。

        口径（与竞品的 `ready_count = total - 401 - quota - error` 一致）：

            就绪 = 总数 - 需重登 - 额度耗尽

        「冷却中」不算坏（等一会儿自己会好），「已禁用」也不是坏（那是人为关掉的）。
        """
        enabled = bool(self.config.get("inspector.circuit_breaker_enabled", True))
        try:
            threshold = float(self.config.get("inspector.min_ready_ratio") or 0.5)
        except (TypeError, ValueError):
            threshold = 0.5
        broken = int(counts.get(STATE_UNAUTHORIZED, 0)) + int(counts.get(STATE_QUOTA_EXHAUSTED, 0))
        total = int(total or 0)
        ready = max(0, total - broken)
        ratio = (ready / total) if total else 1.0
        opened = enabled and total > 0 and ratio < threshold
        return {
            "enabled": enabled, "open": opened,
            "ready": ready, "total": total,
            "ready_ratio": round(ratio, 4), "threshold": threshold,
            "reason": (f"就绪率 {ready}/{total} = {ratio:.0%}，低于阈值 {threshold:.0%}；"
                       f"本轮放弃全部维护动作，避免集体失效时误删（冷却与已禁用不算坏）"
                       if opened else ""),
        }

    def _plan_for(self, item: Dict[str, Any], classified: Dict[str, Any],
                  row: Any) -> List[Dict[str, Any]]:
        name = item.get("name") or ""
        state = classified.get("state")
        runtime_only = as_bool(item.get("runtime_only"))
        planned: List[Dict[str, Any]] = []

        if runtime_only:
            # 插件虚拟凭证不能直接改/删（上游明确拒绝）
            return planned

        # 证据强度闸门：只有字段级证据（evidence_level=strong）才允许不可逆动作。
        # 文案级证据（status_message 里一句 "429 too many requests"）默认只标记 + 告警 ——
        # 想放开必须显式改 inspector.act_on_weak_evidence。
        weak_evidence = (classified.get("evidence_level") or "strong") == "weak"
        act_on_weak = as_bool(self.config.get("inspector.act_on_weak_evidence", False))
        allow_hard = (not weak_evidence) or act_on_weak

        if state == STATE_UNAUTHORIZED:
            if not allow_hard:
                planned.append({"action": ACTION_MARK, "name": name,
                                "reason": (classified.get("reason") or "认证失效") +
                                          "（只有文案证据，按配置仅标记）"})
            elif self.config.get("inspector.delete_unauthorized", False):
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
            if not allow_hard:
                planned.append({"action": ACTION_MARK, "name": name,
                                "reason": (classified.get("reason") or "疑似额度耗尽") +
                                          "（只有文案证据，按配置仅标记）"})
            elif self.config.get("inspector.delete_quota_exhausted", False):
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

    @staticmethod
    def _auth_index_of(action: Dict[str, Any]) -> str:
        """从动作里拿 auth_index：优先看数据库行，其次看动作自带字段。

        `sqlite3.Row` 对不存在的列会抛 IndexError（不是 KeyError），两个都要接。
        """
        row = action.get("_row")
        if row is not None:
            try:
                index = str(row["auth_index"] or "")
            except (KeyError, IndexError, TypeError):
                index = ""
            if index:
                return index
        return str(action.get("auth_index") or "")

    def _execute(self, client: CPAClient, action: Dict[str, Any]) -> tuple:
        name = action.get("name")
        if not name:
            return "skipped", "缺少凭证名"
        try:
            if action["action"] == ACTION_MARK:
                # 标记型动作：只写本地状态与告警，**绝不碰上游**
                return "ok", "仅标记（未改动上游）"
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
            if action["action"] == ACTION_COOLDOWN_RESET:
                auth_index = self._auth_index_of(action)
                if not auth_index:
                    return "skipped", "缺少 auth_index（上游只认 auth_index，不认 name）"
                client.reset_cooldown(auth_index)
                return "ok", "已重置冷却"
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
        elif action["action"] == ACTION_COOLDOWN_RESET:
            # 本地也要同步：否则面板还显示「冷却中」，与上游实际状态不一致
            self.store.ex("UPDATE credentials SET next_retry_after = 0, unavailable = 0 "
                          "WHERE id = ?", (cred_id,))

    # ------------------------------------------------------------------ 手动动作

    def disable_credential(self, node_id: int, name: str) -> Dict[str, Any]:
        return self._manual(node_id, name, ACTION_DISABLE)

    def enable_credential(self, node_id: int, name: str) -> Dict[str, Any]:
        return self._manual(node_id, name, ACTION_ENABLE)

    def delete_credential(self, node_id: int, name: str) -> Dict[str, Any]:
        return self._manual(node_id, name, ACTION_DELETE)

    def reset_cooldown_credential(self, node_id: int, name: str, auth_index: str) -> Dict[str, Any]:
        """把冷却中的凭证立刻放回调度（走上游 `POST /routing/cooldown/reset`）。

        上游只接受 auth_index（不是 name）—— 所以本地没同步到 auth_index 时必须**明说**，
        而不是发一个上游会当成“空”的请求然后假装成功。
        """
        node = self.store.get_node(node_id)
        if not node:
            return {"ok": False, "error": "节点不存在"}
        if not auth_index:
            return {"ok": False, "error": "该凭证缺少 auth_index；上游只接受 auth_index"}
        client = self._client(node)
        try:
            result = client.reset_cooldown(auth_index)
        except CPAError as exc:
            self.store.add_action(None, node_id, name, ACTION_COOLDOWN_RESET, "手动重置冷却",
                                  "failed", str(exc))
            return {"ok": False, "error": str(exc), "status": exc.status}
        self.store.add_action(None, node_id, name, ACTION_COOLDOWN_RESET, "手动重置冷却", "ok", "")
        row = self.store.q1("SELECT * FROM credentials WHERE node_id = ? AND name = ?",
                            (node_id, name))
        if row is not None:
            self._apply_local_state(node_id, {"action": ACTION_COOLDOWN_RESET, "_row": row},
                                    dry_run=False)
        self.store.audit("credential.cooldown_reset", actor="user", target=name)
        return {"ok": True, "result": result}

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

    def client_for(self, node_id: int) -> CPAClient:
        """按节点 id 建客户端。

        给 CLI 这类外部入口用（它们只拿到 `Panel`，没地方建客户端）。
        面板侧走 `PanelApp.client`，但两者底层用的是同一个 `_client`。
        """
        node = self.store.get_node(int(node_id))
        if node is None:
            raise CPAError(f"节点不存在：{node_id}", status=404)
        return self._client(node)

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
