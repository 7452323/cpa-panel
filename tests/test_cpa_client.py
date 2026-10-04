"""CPA 客户端测试：前缀探测、鉴权、错误映射、消费型队列。

这些测试全部对着 Mock CPA 跑（`tests/mock_cpa.py`），它复刻了上游的真实行为：
密钥不对是 401、没配密钥是 404、`count` 非正整数是 400、pop 后记录消失。
"""

import json
import time
import unittest

from cpapanel.cpa import CPAClient, CPAError

from tests.mock_cpa import SAMPLE_CREDENTIALS, SAMPLE_USAGE, MockCPA


class TestPrefixDetection(unittest.TestCase):
    def test_detects_v8(self):
        with MockCPA(prefix="v8") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "auto")
            self.assertEqual(client.resolve_prefix(), "v8")
            self.assertEqual(client.url_for("auth_files"), "/v8/management/credentials")
            self.assertEqual(client.url_for("usage_queue"),
                             "/v8/management/observability/usage/queue")

    def test_falls_back_to_v0(self):
        with MockCPA(prefix="v0") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "auto")
            self.assertEqual(client.resolve_prefix(), "v0")
            self.assertEqual(client.url_for("auth_files"), "/v0/management/auth-files")
            self.assertEqual(client.url_for("usage_queue"), "/v0/management/usage-queue")

    def test_explicit_prefix_is_not_probed(self):
        with MockCPA(prefix="v8") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "v8")
            self.assertEqual(client.resolve_prefix(), "v8")
            self.assertEqual(len(mock.calls()), 0)   # 没有探测请求

    def test_v8_only_path_for_api_keys(self):
        with MockCPA(prefix="v8") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "v8")
            self.assertEqual(client.url_for("api_keys"),
                             "/v8/management/config/access/api-keys")


class TestAuthAndErrors(unittest.TestCase):
    def test_wrong_key_is_401(self):
        with MockCPA(prefix="v8") as mock:
            client = CPAClient(mock.base_url, "wrong-key", "v8")
            with self.assertRaises(CPAError) as ctx:
                client.get_config()
            self.assertEqual(ctx.exception.status, 401)
            self.assertFalse(ctx.exception.management_unavailable)

    def test_no_management_key_upstream_returns_404(self):
        """上游没配管理密钥时不注册路由 —— 必须能识别为「未启用管理 API」。"""
        with MockCPA(prefix="v8", management_key="") as mock:
            client = CPAClient(mock.base_url, "", "auto")
            with self.assertRaises(CPAError) as ctx:
                client.resolve_prefix()
            self.assertEqual(ctx.exception.status, 404)
            self.assertTrue(ctx.exception.management_unavailable)
            self.assertIn("未启用", ctx.exception.to_dict()["hint"])

    def test_both_headers_are_sent(self):
        with MockCPA(prefix="v8") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "v8")
            client.get_config()
            call = mock.calls()[-1]
            self.assertEqual(call["auth"], "Bearer sk-mock-key")
            self.assertEqual(call["mgmt_key"], "sk-mock-key")

    def test_connection_error_is_wrapped(self):
        client = CPAClient("http://127.0.0.1:1", "k", "v8", timeout=(0.5, 0.5))
        with self.assertRaises(CPAError):
            client.get_config()

    def test_probe_reports_hint(self):
        with MockCPA(prefix="v8", management_key="") as mock:
            client = CPAClient(mock.base_url, "", "auto")
            result = client.probe()
            self.assertFalse(result["ok"])
            self.assertTrue(result["management_unavailable"])


class TestAuthFiles(unittest.TestCase):
    def setUp(self):
        self.mock = MockCPA(prefix="v8").start()
        self.mock.seed_credentials(SAMPLE_CREDENTIALS)
        self.client = CPAClient(self.mock.base_url, "sk-mock-key", "v8")

    def tearDown(self):
        self.mock.stop()

    def test_list_without_pagination(self):
        data = self.client.list_auth_files()
        self.assertEqual(len(data["files"]), len(SAMPLE_CREDENTIALS))
        self.assertIsNotNone(data["observed_at"])
        self.assertIsNone(data.get("total"))

    def test_list_with_pagination(self):
        data = self.client.list_auth_files(page=1, page_size=2)
        self.assertEqual(len(data["files"]), 2)
        self.assertEqual(data["total"], len(SAMPLE_CREDENTIALS))
        self.assertTrue(data["has_more"])

    def test_filter_by_name(self):
        data = self.client.list_auth_files(name="codex-1.json")
        self.assertEqual(len(data["files"]), 1)
        self.assertEqual(data["files"][0]["auth_index"], "idx-1")

    def test_models(self):
        models = self.client.auth_file_models("codex-1.json")
        self.assertEqual(models[0]["id"], "gpt-5-codex")

    def test_models_without_name_is_400(self):
        with self.assertRaises(CPAError) as ctx:
            self.client.auth_file_models("")
        self.assertEqual(ctx.exception.status, 400)

    def test_disable_then_delete(self):
        self.client.patch_auth_file_status("codex-1.json", disabled=True)
        self.assertTrue(self.mock.state.find_credential("codex-1.json")["disabled"])
        self.client.patch_auth_file_status("codex-1.json", disabled=False)
        self.assertFalse(self.mock.state.find_credential("codex-1.json")["disabled"])
        self.client.delete_auth_file("codex-4.json")
        self.assertIsNone(self.mock.state.find_credential("codex-4.json"))

    def test_patch_fields(self):
        self.client.patch_auth_file_fields("codex-1.json", {"priority": 5, "note": "backup"})
        target = self.mock.state.find_credential("codex-1.json")
        self.assertEqual(target["priority"], 5)
        self.assertEqual(target["note"], "backup")

    def test_upload(self):
        self.client.upload_auth_file("new-codex.json", b'{"type":"codex"}')
        self.assertIsNotNone(self.mock.state.find_credential("new-codex.json"))

    def test_download(self):
        body = self.client.download_auth_file("codex-1.json")
        self.assertIn(b"mock", body)

    def test_refresh(self):
        self.client.refresh_auth_files(name="codex-1.json")
        self.assertIsNotNone(self.mock.state.find_credential("codex-1.json")["last_refresh"])


class TestUsageQueue(unittest.TestCase):
    def setUp(self):
        self.mock = MockCPA(prefix="v8").start()
        self.client = CPAClient(self.mock.base_url, "sk-mock-key", "v8")

    def tearDown(self):
        self.mock.stop()

    def test_pop_is_destructive(self):
        self.mock.push_usage(*SAMPLE_USAGE)
        size_before = self.mock.queue_size()
        records = self.client.usage_queue(count=10)
        self.assertEqual(len(records), size_before)
        self.assertEqual(self.mock.queue_size(), 0)
        self.assertEqual(self.client.usage_queue(count=10), [])

    def test_count_is_honoured(self):
        self.mock.push_usage(*SAMPLE_USAGE)
        records = self.client.usage_queue(count=2)
        self.assertEqual(len(records), 2)
        self.assertEqual(self.mock.queue_size(), len(SAMPLE_USAGE) - 2)

    def test_expired_records_are_dropped(self):
        """上游只保留 60 秒 —— 超过窗口的记录取不回来。"""
        self.mock.push_usage_aged(120, {"request_id": "stale"})
        self.mock.push_usage({"request_id": "fresh"})
        records = self.client.usage_queue(count=10)
        ids = [r.get("request_id") for r in records]
        self.assertNotIn("stale", ids)
        self.assertIn("fresh", ids)

    def test_count_must_be_positive(self):
        """客户端不会发 0，但上游对非法 count 返回 400 —— 验证契约仍在。"""
        with self.assertRaises(CPAError) as ctx:
            self.client.request("GET", "/v8/management/observability/usage/queue",
                                params={"count": 0})
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("positive integer", ctx.exception.body)
        with self.assertRaises(CPAError) as ctx:
            self.client.request("GET", "/v8/management/observability/usage/queue",
                                params={"count": "abc"})
        self.assertEqual(ctx.exception.status, 400)

    def test_client_always_sends_positive_count(self):
        self.mock.push_usage(*SAMPLE_USAGE)
        self.client.usage_queue(count=0)      # 客户端内部会纠正成 1
        self.assertEqual(mock_call_count(self.mock, "/queue"), 1)


def mock_call_count(mock, fragment):
    return sum(1 for c in mock.calls() if fragment in c["path"])


class TestApiKeys(unittest.TestCase):
    def test_v8_key_list_roundtrip(self):
        with MockCPA(prefix="v8") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "v8")
            self.assertEqual(client.get_api_keys(), [])
            client.put_api_keys(["sk-a", "sk-b"])
            self.assertEqual(client.get_api_keys(), ["sk-a", "sk-b"])
            client.delete_api_keys(["sk-a"])
            self.assertEqual(client.get_api_keys(), ["sk-b"])

    def test_v0_key_list_shape(self):
        with MockCPA(prefix="v0") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "v0")
            client.put_api_keys(["sk-x"])
            self.assertEqual(client.get_api_keys(), ["sk-x"])


class TestFlagsAndRuntime(unittest.TestCase):
    def test_usage_stats_flag_and_latest_version(self):
        with MockCPA(prefix="v0") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "v0")
            self.assertTrue(client.get_usage_stats_enabled())
            client.set_usage_stats_enabled(False)
            self.assertFalse(client.get_usage_stats_enabled())
            version = client.latest_version()
            self.assertEqual(version["version"], "6.9.49")

    def test_probe_ok(self):
        with MockCPA(prefix="v8") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "auto")
            result = client.probe()
            self.assertTrue(result["ok"])
            self.assertEqual(result["prefix"], "v8")
            self.assertTrue(result["usage_statistics_enabled"])


class TestOAuth(unittest.TestCase):
    def test_v8_generic_flow(self):
        with MockCPA(prefix="v8") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "v8")
            started = client.start_oauth("codex")
            self.assertTrue(started["url"].startswith("https://"))
            self.assertTrue(started["state"])
            self.assertEqual(client.oauth_status(started["state"])["status"], "wait")
            client.cancel_oauth(started["state"])
            self.assertEqual(len(mock.state.oauth_sessions), 0)

    def test_v0_provider_specific_flow(self):
        with MockCPA(prefix="v0") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "v0")
            started = client.start_oauth("claude")
            # v0 的 claude 走的是 /anthropic-auth-url（上游历史命名），所以路径里是 anthropic
            self.assertIn("anthropic", started["url"])
            self.assertTrue(started["state"])
            self.assertEqual(client.oauth_status(started["state"])["status"], "wait")

    def test_v0_unknown_provider_is_rejected(self):
        with MockCPA(prefix="v0") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "v0")
            with self.assertRaises(CPAError):
                client.start_oauth("not-a-provider")


class TestKeyWireFormat(unittest.TestCase):
    """下游 Key 的**线上格式**断言（不只是“调用成功了”）。

    官方 WebUI 是 `apiClient.put(PATH, keys)` —— 直接发一个裸 JSON 数组
    （见 `services/api/apiKeys.ts`）。如果客户端发成 `{"api-keys": [...]}`，
    某些上游版本会把它当成“根本没有 api-keys 字段”，后果是**把下游 Key 全部清空**。
    这个 bug 在只看返回值时是完全看不出来的，所以必须断言 body 形状。
    """

    def test_put_sends_bare_array(self):
        import json as _json
        with MockCPA(prefix="v8") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "v8")
            client.put_api_keys(["sk-a", "sk-b"])
            puts = [c for c in mock.calls() if c["method"] == "PUT"]
            self.assertEqual(len(puts), 1, "不应该触发包装体回退")
            self.assertEqual(_json.loads(puts[0]["body"]), ["sk-a", "sk-b"])
            self.assertEqual(mock.state.api_keys, ["sk-a", "sk-b"])

    def test_delete_sends_bare_array(self):
        import json as _json
        with MockCPA(prefix="v8") as mock:
            mock.state.api_keys = ["sk-a", "sk-b"]
            client = CPAClient(mock.base_url, "sk-mock-key", "v8")
            client.delete_api_keys(["sk-a"])
            deletes = [c for c in mock.calls() if c["method"] == "DELETE"]
            self.assertEqual(_json.loads(deletes[0]["body"]), ["sk-a"])
            self.assertEqual(mock.state.api_keys, ["sk-b"])

    def test_v0_also_accepts_bare_array(self):
        """v0 路径同样要能处理裸数组（旧前缀不能成为“静默清空 Key”的漏洞）。"""
        with MockCPA(prefix="v0") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "v0")
            client.put_api_keys(["sk-only"])
            self.assertEqual(mock.state.api_keys, ["sk-only"])


class TestCredentialWrites(unittest.TestCase):
    """凭证写操作的**线上形状**断言（对着官方 WebUI 的请求体）。

    这些都不是“调用成功了就行”的测试："names" 写成 "name"、auth_index 漏传，
    上游都可能返回 200 却什么都没干（或者删错东西），只有断言 body 才治得住。
    """

    def test_delete_sends_names_body(self):
        with MockCPA(prefix="v8") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "v8")
            mock.seed_credentials(SAMPLE_CREDENTIALS)
            client.delete_auth_file("codex-1.json")
            dels = [c for c in mock.calls() if c["method"] == "DELETE"]
            self.assertEqual(len(dels), 1, "不应触发 query 回退")
            self.assertEqual(json.loads(dels[0]["body"]), {"names": ["codex-1.json"]})
            self.assertIsNone(mock.state.find_credential("codex-1.json"))

    def test_batch_delete_only_touches_listed(self):
        with MockCPA(prefix="v8") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "v8")
            mock.seed_credentials(SAMPLE_CREDENTIALS)
            client.delete_auth_files(["codex-1.json", "codex-2.json"])
            self.assertIsNone(mock.state.find_credential("codex-1.json"))
            self.assertIsNone(mock.state.find_credential("codex-2.json"))
            self.assertIsNotNone(mock.state.find_credential("codex-3.json"))

    def test_delete_all_uses_all_flag(self):
        with MockCPA(prefix="v8") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "v8")
            mock.seed_credentials(SAMPLE_CREDENTIALS)
            client.delete_all_auth_files()
            self.assertEqual(len(mock.state.credentials), 0)
            dels = [c for c in mock.calls() if c["method"] == "DELETE"]
            self.assertEqual(dels[0]["query"].get("all"), "true")

    def test_reset_cooldown_clears_next_retry(self):
        with MockCPA(prefix="v8") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "v8")
            mock.seed_credentials(SAMPLE_CREDENTIALS)
            cooling = mock.state.find_credential("codex-3.json")
            self.assertGreater(int(cooling["next_retry_after"]), 0)
            expected_index = cooling["auth_index"]
            client.reset_cooldown(expected_index)
            after = mock.state.find_credential("codex-3.json")
            self.assertEqual(int(after["next_retry_after"]), 0)
            resets = [c for c in mock.calls() if c["path"].endswith("/routing/cooldown/reset")]
            self.assertEqual(json.loads(resets[0]["body"]), {"auth_index": expected_index})

    def test_reset_cooldown_without_auth_index_is_rejected_locally(self):
        with MockCPA(prefix="v8") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "v8")
            with self.assertRaises(CPAError) as ctx:
                client.reset_cooldown("")
            self.assertEqual(ctx.exception.status, 400)
            self.assertEqual(len(mock.calls()), 0, "本地就该拦住，不发无效请求")

    def test_reset_cooldown_on_v0_is_501_not_404(self):
        """冷却重置是 v8 能力。v0 连接下要**明说支持不了**，
        而不是发一个必然 404 的请求，让用户去猜是路径写错了还是上游没开。"""
        with MockCPA(prefix="v0") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "v0")
            with self.assertRaises(CPAError) as ctx:
                client.reset_cooldown("idx-3")
            self.assertEqual(ctx.exception.status, 501)

    def test_refresh_all_sends_all_flag(self):
        with MockCPA(prefix="v8") as mock:
            client = CPAClient(mock.base_url, "sk-mock-key", "v8")
            client.refresh_auth_files(all_=True)
            posts = [c for c in mock.calls() if c["method"] == "POST"]
            self.assertEqual(json.loads(posts[0]["body"]), {"all": True})


if __name__ == "__main__":
    unittest.main()
