"""通知出口：Webhook / Telegram。

原则：通知失败绝不影响主流程（采集与巡检不能因为发不出告警就崩），
并且同一个事件在 `min_interval_seconds` 内只发一次（去抖），避免刷屏。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .log import get
from .util import jdump, now_ts, short

log = get("cpapanel.notify")

EVENTS = (
    "inspection.unauthorized",
    "inspection.quota_exhausted",
    "inspection.low_pool",
    "inspection.error",
    "collector.gap",
    "collector.error",
    "panel.start",
)


class Notifier:
    def __init__(self, store: Any, config: Any):
        self.store = store
        self.config = config

    # ------------------------------------------------------------------ 对外

    def notify(self, event: str, title: str, text: str = "", payload: Optional[Dict[str, Any]] = None,
               force: bool = False) -> Dict[str, Any]:
        events = self.config.get("notify.events") or []
        if not force and event not in events:
            return {"sent": False, "reason": "事件未订阅"}
        if self._debounced(event, force):
            return {"sent": False, "reason": "去抖窗口内已发送"}
        result: Dict[str, Any] = {"sent": False, "channels": []}
        body = {
            "event": event,
            "title": title,
            "text": text,
            "ts": now_ts(),
            "panel": "cpa-panel",
            "payload": payload or {},
        }
        webhook = self.config.get("notify.webhook_url") or ""
        if webhook:
            ok, detail = self._post_json(webhook, body)
            result["channels"].append({"channel": "webhook", "ok": ok, "detail": detail})
            result["sent"] = result["sent"] or ok
        token = self.config.get("notify.telegram_bot_token") or ""
        chat = str(self.config.get("notify.telegram_chat_id") or "")
        if token and chat:
            message = f"*{title}*\n{text}" if text else f"*{title}*"
            ok, detail = self._post_json(
                f"https://api.telegram.org/bot{token}/sendMessage",
                {"chat_id": chat, "text": message, "parse_mode": "Markdown",
                 "disable_web_page_preview": True})
            result["channels"].append({"channel": "telegram", "ok": ok, "detail": detail})
            result["sent"] = result["sent"] or ok
        if not result["channels"]:
            result["reason"] = "未配置任何通知渠道"
        return result

    def test(self) -> Dict[str, Any]:
        return self.notify("panel.start", "cpa-panel 测试通知",
                           "如果你看到这条消息，说明通知渠道配置正确。", force=True)

    # ------------------------------------------------------------------ 内部

    def _debounced(self, event: str, force: bool) -> bool:
        if force:
            return False
        window = int(self.config.get("notify.min_interval_seconds") or 300)
        if window <= 0:
            return False
        last = self.store.get_setting(f"notify_last.{event}")
        now = now_ts()
        if last and now - int(last) < window:
            return True
        self.store.set_setting(f"notify_last.{event}", now)
        return False

    def _post_json(self, url: str, body: Dict[str, Any]) -> tuple:
        data = jdump(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "cpa-panel/0.1"})
        timeout = float(self.config.get("http.read_timeout") or 20)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read(2000).decode("utf-8", "replace")
                ok = 200 <= resp.status < 300
                if not ok:
                    log.warning("通知 %s 返回 %s: %s", short(url, 60), resp.status, short(text, 200))
                return ok, f"{resp.status} {short(text, 160)}"
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(500).decode("utf-8", "replace")
            except Exception:
                pass
            log.warning("通知 %s 失败：HTTP %s %s", short(url, 60), exc.code, short(detail, 160))
            return False, f"{exc.code} {short(detail, 160)}"
        except Exception as exc:  # noqa: BLE001 - 通知必须永不抛出
            log.warning("通知 %s 失败：%s", short(url, 60), exc)
            return False, str(exc)


def summarize_inspection(result: Dict[str, Any]) -> str:
    """把巡检结果压成一条可推送的短消息。"""
    counts = result.get("counts") or {}
    lines = [
        f"节点: {result.get('node_name') or result.get('node_id')}",
        f"扫描: {counts.get('total', 0)} 个凭证",
        f"健康: {counts.get('healthy', 0)} / 冷却: {counts.get('cooling', 0)}",
        f"额度耗尽: {counts.get('quota_exhausted', 0)} / 需重登: {counts.get('unauthorized', 0)}",
        f"已禁用: {counts.get('disabled', 0)} / 备用池: {counts.get('standby', 0)}",
        f"本次动作: {result.get('executed', 0)} 项（计划 {result.get('planned', 0)} 项）",
    ]
    if result.get("dry_run"):
        lines.append("⚠️ dry-run：只做计划，未改动上游")
    if result.get("executed_detail"):
        lines.append("明细: " + json.dumps(result["executed_detail"], ensure_ascii=False)[:400])
    return "\n".join(lines)
