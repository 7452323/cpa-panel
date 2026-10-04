"""端到端集成测试：真实 HTTP 链路。

链路：Mock CPA（复刻上游语义） ← CPAClient ← 面板后端 ← 面板 HTTP API ← 测试 HTTP 客户端。

覆盖的关键行为：
* 用量队列「pop 即删除」→ 面板必须落库；重复推送必须被去重；
* 超过 60s 保留窗口的记录取不回来；
* 巡检 dry-run 不改上游、apply 才改；
* 鉴权（会话 + 令牌 + CSRF）与密钥脱敏。
"""

import http.cookiejar
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from cpapanel import runtime
from cpapanel.config import Config
from cpapanel.security import new_token, token_hash
from cpapanel.web.server import serve

from tests.mock_cpa import SAMPLE_CREDENTIALS, SAMPLE_USAGE, MockCPA

ADMIN_USER = "admin"
ADMIN_PASSWORD = "test-password-123"

FIXTURE = {}


class Client:
    """极简 HTTP 客户端（带 Cookie 罐），用于打面板自己的 API。"""

    def __init__(self, base: str, token: str = None):
        self.base = base.rstrip("/")
        self.token = token
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method: str, path: str, payload=None, csrf: bool = True,
             auth: bool = True):
        headers = {}
        if csrf:
            headers["X-CPA-Panel"] = "1"
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers,
                                     method=method.upper())
        try:
            with self.opener.open(req, timeout=20) as resp:
                body, status = resp.read(), resp.status
        except urllib.error.HTTPError as exc:
            body, status = exc.read(), exc.code
        text = body.decode("utf-8", "replace")
        try:
            parsed = json.loads(text) if text else None
        except ValueError:
            parsed = text
        return status, parsed


def setUpModule():
    mock = MockCPA(prefix="v8").start()
    mock.seed_credentials(SAMPLE_CREDENTIALS)

    tmpdir = tempfile.mkdtemp(prefix="cpa-panel-e2e-")
    config = Config(data={
        "host": "127.0.0.1", "port": 0,
        "data_dir": tmpdir, "database": os.path.join(tmpdir, "panel.db"),
        "log_file": "",
        "collector": {"enabled": False, "interval_seconds": 15, "batch_size": 50},
        "inspector": {"enabled": False, "dry_run": True},
    })
    config.path = os.path.join(tmpdir, "panel.config.json")
    config.save()

    panel = runtime.build(config, setup_logging=False)
    node_id = panel.store.add_node("mock-v8", mock.base_url, "sk-mock-key", "auto")
    bad_node_id = panel.store.add_node("mock-bad-key", mock.base_url, "wrong-key", "v8")
    runtime.ensure_admin(panel.store, config, ADMIN_USER, ADMIN_PASSWORD)

    raw_token = new_token()
    panel.store.create_panel_token("e2e", token_hash(raw_token), "admin")
    viewer_token = new_token()
    panel.store.create_panel_token("e2e-viewer", token_hash(viewer_token), "viewer")

    httpd = serve(panel.app, "127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    FIXTURE.update({
        "mock": mock, "panel": panel, "config": config, "tmpdir": tmpdir,
        "node_id": node_id, "bad_node_id": bad_node_id,
        "base": f"http://127.0.0.1:{port}", "httpd": httpd, "thread": thread,
        "token": raw_token, "viewer_token": viewer_token,
    })


def tearDownModule():
    FIXTURE["httpd"].shutdown()
    FIXTURE["httpd"].server_close()
    FIXTURE["panel"].close()
    FIXTURE["mock"].stop()


def api():  # 带管理员令牌的客户端
    return Client(FIXTURE["base"], FIXTURE["token"])


# --------------------------------------------------------------------------- 基础


class TestPublicEndpoints(unittest.TestCase):
    def test_health_needs_no_auth(self):
        status, body = Client(FIXTURE["base"]).call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["app"], "cpa-panel")

    def test_protected_endpoint_rejects_anonymous(self):
        status, body = Client(FIXTURE["base"]).call("GET", "/api/overview")
        self.assertEqual(status, 401)
        self.assertIn("未登录", body["error"])

    def test_metrics_is_prometheus_text(self):
        status, body = Client(FIXTURE["base"]).call("GET", "/metrics")
        self.assertEqual(status, 200)
        self.assertIn("cpa_panel_up 1", body)
        self.assertIn("cpa_panel_credentials", body)

    def test_spa_fallback_serves_index(self):
        status, body = Client(FIXTURE["base"]).call("GET", "/accounts")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, str)
        self.assertIn("<html", body.lower())


class TestAuthAndCsrf(unittest.TestCase):
    def test_session_login_flow(self):
        client = Client(FIXTURE["base"], token=None)
        status, body = client.call("POST", "/api/login",
                                   {"username": ADMIN_USER, "password": ADMIN_PASSWORD})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, body = client.call("GET", "/api/session")
        self.assertEqual(status, 200)
        self.assertTrue(body["authenticated"])
        self.assertEqual(body["username"], ADMIN_USER)
        status, _ = client.call("POST", "/api/logout")
        self.assertEqual(status, 200)
        self.assertFalse(client.call("GET", "/api/session")[1]["authenticated"])

    def test_wrong_password_rejected(self):
        client = Client(FIXTURE["base"], token=None)
        status, body = client.call("POST", "/api/login",
                                   {"username": ADMIN_USER, "password": "nope"})
        self.assertEqual(status, 401)
        self.assertIn("用户名或密码错误", body["error"])

    def test_csrf_header_is_required_for_session_writes(self):
        client = Client(FIXTURE["base"], token=None)
        client.call("POST", "/api/login", {"username": ADMIN_USER, "password": ADMIN_PASSWORD})
        status, body = client.call("PUT", "/api/settings", {"inspector.auto": True}, csrf=False)
        self.assertEqual(status, 403)
        self.assertIn("CSRF", body["error"])
        status, _ = client.call("PUT", "/api/settings", {"inspector.auto": True}, csrf=True)
        self.assertEqual(status, 200)

    def test_bearer_token_works(self):
        status, body = api().call("GET", "/api/overview")
        self.assertEqual(status, 200)
        self.assertIn("nodes", body)

    def test_viewer_token_cannot_write(self):
        viewer = Client(FIXTURE["base"], FIXTURE["viewer_token"])
        self.assertEqual(viewer.call("GET", "/api/overview")[0], 200)
        status, body = viewer.call("PUT", "/api/config", {"log_level": "DEBUG"})
        self.assertEqual(status, 403)
        self.assertIn("管理员", body["error"])

    def test_bad_token_is_anonymous(self):
        status, _ = Client(FIXTURE["base"], "not-a-real-token").call("GET", "/api/overview")
        self.assertEqual(status, 401)


# --------------------------------------------------------------------------- 节点


class TestNodes(unittest.TestCase):
    def test_test_connection_success(self):
        status, body = api().call("POST", f"/api/nodes/{FIXTURE['node_id']}/test")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["prefix"], "v8")
        self.assertTrue(body["usage_statistics_enabled"])

    def test_test_connection_wrong_key_gives_hint(self):
        status, body = api().call("POST", f"/api/nodes/{FIXTURE['bad_node_id']}/test")
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertEqual(body["status"], 401)
        self.assertIn("管理密钥", body["hint"])

    def test_node_list_masks_management_key(self):
        status, body = api().call("GET", "/api/nodes")
        self.assertEqual(status, 200)
        node = [n for n in body["nodes"] if n["id"] == FIXTURE["node_id"]][0]
        self.assertNotEqual(node["management_key"], "sk-mock-key")
        self.assertIn("…", node["management_key"])
        self.assertTrue(node["has_key"])


# --------------------------------------------------------------------------- 凭证


class TestCredentials(unittest.TestCase):
    def setUp(self):
        # 先把上游凭证同步进库：这些用例都依赖库里已有数据，
        # 不能指望“别的测试类先跑过”（unittest 的类内方法是按字母序跑的）。
        status, _ = api().call("POST", "/api/credentials/sync",
                               {"node_id": FIXTURE["node_id"]})
        self.assertEqual(status, 200)

    def test_sync_pulls_all_credentials(self):
        status, body = api().call("POST", "/api/credentials/sync",
                                  {"node_id": FIXTURE["node_id"]})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["total"], len(SAMPLE_CREDENTIALS))
        status, listing = api().call("GET", "/api/credentials")
        self.assertEqual(listing["total"], len(SAMPLE_CREDENTIALS))
        self.assertEqual(listing["counts"]["total"], len(SAMPLE_CREDENTIALS))

    def test_state_filter_exposes_unauthorized(self):
        status, body = api().call("GET", "/api/credentials?state=unauthorized")
        self.assertEqual(status, 200)
        names = [c["name"] for c in body["credentials"]]
        self.assertIn("codex-2.json", names)

    def test_credential_view_hides_raw_json(self):
        _, listing = api().call("GET", "/api/credentials?q=codex-2")
        cred = listing["credentials"][0]
        self.assertNotIn("raw_json", cred)
        self.assertEqual(cred["state"], "unauthorized")
        self.assertTrue(cred["state_reason"])

    def test_models_via_panel(self):
        _, listing = api().call("GET", "/api/credentials?q=codex-1")
        cred_id = listing["credentials"][0]["id"]
        status, body = api().call("GET", f"/api/credentials/{cred_id}/models")
        self.assertEqual(status, 200)
        self.assertEqual(body["models"][0]["id"], "gpt-5-codex")

    def test_patch_pushes_to_upstream(self):
        _, listing = api().call("GET", "/api/credentials?q=claude-1")
        cred_id = listing["credentials"][0]["id"]
        status, body = api().call("PATCH", f"/api/credentials/{cred_id}", {"priority": 7})
        self.assertEqual(status, 200)
        self.assertEqual(body["pushed"]["priority"], 7)
        target = FIXTURE["mock"].state.find_credential("claude-1.json")
        self.assertEqual(target["priority"], 7)

    def test_manual_disable_then_enable(self):
        _, listing = api().call("GET", "/api/credentials?q=claude-1")
        cred_id = listing["credentials"][0]["id"]
        status, body = api().call("POST", f"/api/credentials/{cred_id}/action",
                                  {"action": "disable"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(FIXTURE["mock"].state.find_credential("claude-1.json")["disabled"])
        status, body = api().call("POST", f"/api/credentials/{cred_id}/action",
                                  {"action": "enable"})
        self.assertTrue(body["ok"])
        self.assertFalse(FIXTURE["mock"].state.find_credential("claude-1.json")["disabled"])

    def test_sync_detects_new_and_missing(self):
        before = FIXTURE["mock"].state.credentials
        FIXTURE["mock"].seed_credentials(before + [
            {"id": "new", "auth_index": "idx-new", "name": "new-codex.json",
             "type": "codex", "provider": "codex", "status": "active", "disabled": False,
             "unavailable": False, "id_token": {}}])
        status, body = api().call("POST", "/api/credentials/sync",
                                  {"node_id": FIXTURE["node_id"]})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["added"], 1)
        FIXTURE["mock"].seed_credentials(before)
        status, body = api().call("POST", "/api/credentials/sync",
                                  {"node_id": FIXTURE["node_id"]})
        self.assertGreaterEqual(body["removed"], 1)


# --------------------------------------------------------------------------- 用量核心链路


class TestUsagePipeline(unittest.TestCase):
    def test_collect_pop_and_persist(self):
        mock = FIXTURE["mock"]
        mock.push_usage(*SAMPLE_USAGE)
        self.assertEqual(mock.queue_size(), len(SAMPLE_USAGE))

        status, body = api().call("POST", "/api/usage/collect",
                                  {"node_id": FIXTURE["node_id"]})
        self.assertEqual(status, 200)
        node_result = body["result"]["nodes"][0]
        self.assertTrue(node_result["ok"])
        self.assertEqual(node_result["fetched"], len(SAMPLE_USAGE))
        # 控制帧 {"support_refresh": true} 必须被忽略 → 只落 3 条
        self.assertEqual(node_result["inserted"], 3)
        self.assertEqual(node_result["duplicates"], 0)
        # 队列已被消费
        self.assertEqual(mock.queue_size(), 0)

    def test_second_push_of_same_records_is_deduped(self):
        mock = FIXTURE["mock"]
        mock.push_usage(*SAMPLE_USAGE)
        status, body = api().call("POST", "/api/usage/collect", {"node_id": FIXTURE["node_id"]})
        result = body["result"]["nodes"][0]
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["duplicates"], 3)

    def test_expired_records_are_lost_forever(self):
        """超过 60s 保留窗口的记录在 pop 之前就被上游丢弃了 —— 取都取不到。"""
        mock = FIXTURE["mock"]
        mock.push_usage_aged(180, {"request_id": "totally-lost", "model": "gpt-5-codex",
                                   "prompt_tokens": 1})
        status, body = api().call("POST", "/api/usage/collect", {"node_id": FIXTURE["node_id"]})
        result = body["result"]["nodes"][0]
        self.assertEqual(result["fetched"], 0)
        self.assertEqual(result["inserted"], 0)

    def test_events_and_summary(self):
        status, body = api().call("GET", "/api/usage/events?limit=50")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(body["events"]), 3)
        event = body["events"][0]
        # 列表接口只给掩码，不给明文 Key，也不给原始 JSON
        self.assertIn("api_key_masked", event)
        self.assertNotIn("api_key", event)
        self.assertNotIn("raw_json", event)

        status, summary = api().call("GET", "/api/usage/summary?days=1")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(summary["summary"]["requests"], 3)
        self.assertGreater(summary["summary"]["cost_usd"], 0)
        self.assertGreater(summary["today"]["total_tokens"], 0)

    def test_series_and_groupings(self):
        _, series = api().call("GET", "/api/usage/series?days=7")
        self.assertTrue(series["series"])
        _, models = api().call("GET", "/api/usage/models?days=7")
        names = [m["model"] for m in models["models"]]
        self.assertIn("gpt-5-codex", names)
        _, creds = api().call("GET", "/api/usage/credentials?days=7")
        self.assertTrue(creds["credentials"])
        self.assertEqual(creds["credentials"][0]["name"], "codex-1.json")

    def test_error_event_detail(self):
        _, events = api().call("GET", "/api/usage/events?errors_only=true")
        self.assertGreaterEqual(len(events["events"]), 1)
        event_id = events["events"][0]["id"]
        status, body = api().call("GET", f"/api/usage/events/{event_id}")
        self.assertEqual(status, 200)
        self.assertIn("raw", body)

    def test_nested_and_seconds_latency_normalized(self):
        """req-3 用的是嵌套 usage + 秒级 duration，验证别名层真的生效。"""
        _, events = api().call("GET", "/api/usage/events?limit=200")
        target = [e for e in events["events"] if e["model"] == "gpt-5-codex"
                  and e["input_tokens"] == 2000]
        self.assertTrue(target, "嵌套 usage 未被归一化")
        self.assertEqual(target[0]["output_tokens"], 1000)
        self.assertEqual(target[0]["latency_ms"], 2500)

    def test_overview_and_alerts(self):
        status, body = api().call("GET", "/api/overview")
        self.assertEqual(status, 200)
        self.assertIn("alerts", body)
        self.assertTrue(body["usage"]["summary"]["requests"] >= 3)
        self.assertTrue(body["nodes"])


# --------------------------------------------------------------------------- 巡检


class TestInspection(unittest.TestCase):
    def test_dry_run_changes_nothing(self):
        mock = FIXTURE["mock"]
        # 把额度耗尽的号恢复成未禁用，保证本轮确实有「计划」可出
        target = mock.state.find_credential("gemini-1.json")
        target["disabled"] = False
        target["status"] = "active"
        status, body = api().call("POST", "/api/inspections/run",
                                  {"node_id": FIXTURE["node_id"], "dry_run": True})
        self.assertEqual(status, 200)
        node = body["result"]["nodes"][0]
        self.assertTrue(node["ok"])
        self.assertTrue(node["dry_run"])
        self.assertGreaterEqual(node["planned"], 1)
        self.assertEqual(node["executed"], 0)
        self.assertFalse(mock.state.find_credential("gemini-1.json")["disabled"])
        # dry-run 也不应改动本地元数据
        _, listing = api().call("GET", "/api/credentials?q=gemini-1")
        self.assertFalse(listing["credentials"][0]["disabled"])

    def test_apply_executes_and_persists_state(self):
        mock = FIXTURE["mock"]
        status, body = api().call("POST", "/api/inspections/run",
                                  {"node_id": FIXTURE["node_id"], "dry_run": False})
        self.assertEqual(status, 200)
        node = body["result"]["nodes"][0]
        self.assertGreaterEqual(node["executed"], 1)
        # 额度耗尽的号按默认配置禁用
        self.assertTrue(mock.state.find_credential("gemini-1.json")["disabled"])
        # 需重登的号进备用池（本地标记 + 上游禁用）
        self.assertTrue(mock.state.find_credential("codex-2.json")["disabled"])
        _, listing = api().call("GET", "/api/credentials?standby=true")
        standby_names = [c["name"] for c in listing["credentials"]]
        self.assertIn("codex-2.json", standby_names)
        # 冷却中的号不该被动
        self.assertFalse(mock.state.find_credential("codex-3.json")["disabled"])

    def test_counts_and_history_recorded(self):
        _, body = api().call("GET", "/api/inspections?limit=10")
        self.assertGreaterEqual(len(body["inspections"]), 1)
        item = body["inspections"][0]
        self.assertEqual(item["scanned"], len(SAMPLE_CREDENTIALS))
        self.assertGreaterEqual(item["unauthorized"], 1)
        _, actions = api().call("GET", "/api/actions?limit=50")
        self.assertGreaterEqual(len(actions["actions"]), 1)

    def test_samples_are_recorded(self):
        _, listing = api().call("GET", "/api/credentials?q=codex-2")
        cred_id = listing["credentials"][0]["id"]
        _, body = api().call("GET", f"/api/credentials/{cred_id}")
        self.assertGreaterEqual(len(body["credential"]["samples"]), 2)


# --------------------------------------------------------------------------- Key / 配置 / 日志


class TestKeysAndConfig(unittest.TestCase):
    def test_key_lifecycle(self):
        mock = FIXTURE["mock"]
        status, body = api().call("POST", "/api/keys",
                                  {"node_id": FIXTURE["node_id"], "key": "sk-e2e-key-1"})
        self.assertEqual(status, 200)
        self.assertIn("sk-e2e-key-1", mock.state.api_keys)

        status, body = api().call("POST", "/api/keys/sync", {"node_id": FIXTURE["node_id"]})
        self.assertEqual(status, 200)
        _, listing = api().call("GET", "/api/keys")
        self.assertGreaterEqual(len(listing["keys"]), 1)
        # 列出的一定是掩码值，不能回明文
        for item in listing["keys"]:
            self.assertNotIn("sk-e2e-key-1", item["key_masked"])
            self.assertIn("*", item["key_masked"])

        status, _ = api().call("DELETE", "/api/keys",
                               {"node_id": FIXTURE["node_id"], "key": "sk-e2e-key-1"})
        self.assertEqual(status, 200)
        self.assertNotIn("sk-e2e-key-1", mock.state.api_keys)

    def test_panel_config_is_redacted(self):
        status, body = api().call("GET", "/api/config")
        self.assertEqual(status, 200)
        # 口令哈希绝不能出现在响应里（既不能是明文哈希，也不能泄露迭代参数）
        self.assertNotIn("pbkdf2", json.dumps(body))
        self.assertGreater(body["pricing_models"], 5)

    def test_config_update_only_whitelisted_keys(self):
        status, body = api().call("PUT", "/api/config",
                                  {"collector.interval_seconds": 30,
                                   "database": "/etc/passwd"})
        self.assertEqual(status, 200)
        self.assertIn("collector.interval_seconds", body["applied"])
        self.assertNotIn("database", body["applied"])
        self.assertEqual(FIXTURE["config"].get("collector.interval_seconds"), 30)
        self.assertNotEqual(FIXTURE["config"].get("database"), "/etc/passwd")

    def test_dry_run_toggle(self):
        status, body = api().call("PUT", "/api/config", {"inspector.dry_run": False})
        self.assertEqual(status, 200)
        self.assertFalse(FIXTURE["config"].get("inspector.dry_run"))
        api().call("PUT", "/api/config", {"inspector.dry_run": True})

    def test_upstream_config_and_yaml(self):
        status, body = api().call("GET", "/api/upstream/config")
        self.assertEqual(status, 200)
        self.assertEqual(body["config"]["port"], 8317)
        status, body = api().call("GET", "/api/upstream/config-yaml")
        self.assertEqual(status, 200)
        self.assertIn("port", body["yaml"])

    def test_upstream_config_replace(self):
        status, body = api().call("PUT", "/api/upstream/config-yaml",
                                  {"yaml": "port: 8317\n", "node_id": FIXTURE["node_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(FIXTURE["mock"].state.config["_yaml"], "port: 8317\n")

    def test_panel_logs_and_audit(self):
        status, body = api().call("GET", "/api/logs/panel?limit=50")
        self.assertEqual(status, 200)
        self.assertIn("lines", body)
        status, body = api().call("GET", "/api/audit?limit=50")
        self.assertEqual(status, 200)
        actions = {a["action"] for a in body["audit"]}
        self.assertTrue(actions & {"credentials.synced", "inspection.run", "key.added",
                                   "config.updated", "usage.manual_collect"})

    def test_maintenance_prune(self):
        status, body = api().call("POST", "/api/maintenance/prune", {"usage_days": 3650})
        self.assertEqual(status, 200)
        self.assertIn("deleted", body)

    def test_settings_roundtrip(self):
        status, _ = api().call("PUT", "/api/settings", {"inspector.auto": False})
        self.assertEqual(status, 200)
        _, body = api().call("GET", "/api/settings")
        self.assertFalse(body["auto_inspect"])
        api().call("PUT", "/api/settings", {"inspector.auto": True})

    def test_oauth_flow(self):
        status, body = api().call("POST", "/api/oauth/start",
                                  {"node_id": FIXTURE["node_id"], "provider": "codex"})
        self.assertEqual(status, 200)
        self.assertTrue(body["url"].startswith("https://"))
        state = body["state"]
        status, body = api().call("GET", f"/api/oauth/status?state={state}")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "wait")
        status, _ = api().call("DELETE", f"/api/oauth/session?state={state}")
        self.assertEqual(status, 200)

    def test_oauth_rejects_unknown_provider(self):
        status, body = api().call("POST", "/api/oauth/start", {"provider": "nope"})
        self.assertEqual(status, 400)


# --------------------------------------------------------------------------- 完整流程


class TestFullWorkflow(unittest.TestCase):
    """模拟一次真实运维：配节点 → 同步 → 采集 → 巡检 → 看报表。"""

    def test_end_to_end(self):
        mock = FIXTURE["mock"]
        client = api()

        self.assertEqual(client.call("POST", f"/api/nodes/{FIXTURE['node_id']}/test")[0], 200)
        self.assertEqual(client.call("POST", "/api/credentials/sync",
                                    {"node_id": FIXTURE["node_id"]})[0], 200)

        mock.push_usage({
            "request_id": "workflow-1", "model": "gemini-2.5-pro", "provider": "gemini",
            "auth_index": "idx-4", "api_key": "sk-workflow", "status_code": 200,
            "prompt_tokens": 1234, "completion_tokens": 567,
        })
        status, body = client.call("POST", "/api/usage/collect",
                                   {"node_id": FIXTURE["node_id"]})
        self.assertEqual(body["result"]["nodes"][0]["inserted"], 1)

        status, body = client.call("GET", "/api/overview")
        self.assertEqual(status, 200)
        self.assertTrue(body["alerts"] is not None)

        status, body = client.call("POST", "/api/inspections/run",
                                   {"node_id": FIXTURE["node_id"], "dry_run": True})
        self.assertTrue(body["result"]["nodes"][0]["planned"] >= 1)

        _, series = client.call("GET", "/api/usage/series?days=7")
        total_requests = sum(item["requests"] for item in series["series"])
        self.assertGreaterEqual(total_requests, 1)
        # 刚采的那条记录必须能按模型查到（验证采集→聚合→报表整条链）
        _, models = client.call("GET", "/api/usage/models?days=7")
        self.assertIn("gemini-2.5-pro", [m["model"] for m in models["models"]])


if __name__ == "__main__":
    unittest.main()
