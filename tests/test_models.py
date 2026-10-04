"""模型层测试：时间解析、状态机、用量归一化、计价。"""

import time
import unittest

from cpapanel.models import (STATE_COOLING, STATE_DISABLED, STATE_HEALTHY, STATE_QUOTA_EXHAUSTED,
                             STATE_UNAUTHORIZED, classify_credential, credential_view,
                             normalize_auth_file, normalize_usage_record, quality_report)
from cpapanel.pricing import Pricing
from cpapanel.util import parse_ts


class TestParseTs(unittest.TestCase):
    def test_unix_seconds_and_millis(self):
        self.assertEqual(parse_ts(1700000000), 1700000000)
        self.assertEqual(parse_ts(1700000000000), 1700000000)

    def test_iso_and_plain(self):
        self.assertEqual(parse_ts("2026-10-05T00:00:00Z"), 1791158400)
        self.assertEqual(parse_ts("2026-10-05 00:00:00"), 1791158400)
        self.assertEqual(parse_ts("2026-10-05"), 1791158400)

    def test_invalid(self):
        for value in (None, "", "not-a-time", True, 0, -5):
            self.assertIsNone(parse_ts(value))


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time())

    def test_disabled_wins(self):
        result = classify_credential({"disabled": True, "status": "active",
                                      "status_message": "token expired"}, self.now)
        self.assertEqual(result["state"], STATE_DISABLED)

    def test_unauthorized_from_message(self):
        result = classify_credential({"status": "error", "status_message": "token expired"}, self.now)
        self.assertEqual(result["state"], STATE_UNAUTHORIZED)
        self.assertFalse(result["recoverable"])

    def test_unauthorized_from_expired_subscription(self):
        result = classify_credential({
            "status": "active",
            "id_token": {"chatgpt_subscription_active_until": self.now - 3600},
        }, self.now)
        self.assertEqual(result["state"], STATE_UNAUTHORIZED)

    def test_quota_exhausted_from_signals(self):
        result = classify_credential({
            "status": "active",
            "quota": {"signals": {"quota_exhausted": True, "other": False}},
        }, self.now)
        self.assertEqual(result["state"], STATE_QUOTA_EXHAUSTED)
        self.assertTrue(result["recoverable"])

    def test_cooling_from_next_retry(self):
        result = classify_credential({"unavailable": True,
                                      "next_retry_after": self.now + 600}, self.now)
        self.assertEqual(result["state"], STATE_COOLING)

    def test_healthy(self):
        self.assertEqual(classify_credential({"status": "active"}, self.now)["state"], STATE_HEALTHY)

    def test_error_unknown_reason_is_unknown(self):
        result = classify_credential({"status": "error"}, self.now)
        self.assertEqual(result["state"], "unknown")


class TestNormalizeAuthFile(unittest.TestCase):
    def test_maps_core_fields(self):
        entry = {
            "name": "codex-1.json", "auth_index": "idx-1", "type": "codex",
            "status": "error", "status_message": "token expired", "disabled": False,
            "unavailable": False, "success": 3, "failed": 2,
            "last_refresh": "2026-10-05T00:00:00Z",
            "next_retry_after": int(time.time()) + 60,
            "id_token": {"plan_type": "plus",
                         "chatgpt_subscription_active_until": int(time.time()) + 86400},
        }
        item = normalize_auth_file(entry)
        self.assertEqual(item["name"], "codex-1.json")
        self.assertEqual(item["provider"], "codex")          # type → provider
        self.assertEqual(item["plan_type"], "plus")
        self.assertEqual(item["success"], 3)
        self.assertEqual(item["disabled"], 0)
        self.assertIsNotNone(item["subscription_until"])
        self.assertEqual(item["_classified"]["state"], STATE_UNAUTHORIZED)
        self.assertIn("raw_json", item)

    def test_disabled_flag_is_int(self):
        item = normalize_auth_file({"name": "a.json", "disabled": True})
        self.assertEqual(item["disabled"], 1)

    def test_credential_view_hides_raw(self):
        view = credential_view({
            "id": 1, "name": "a.json", "disabled": 0, "unavailable": 1, "standby": 0,
            "status": "active", "status_message": "", "next_retry_after": int(time.time()) + 60,
            "quota_json": None, "raw_json": "{\"secret\":1}", "subscription_until": None,
        })
        self.assertNotIn("raw_json", view)
        self.assertNotIn("quota_json", view)
        self.assertTrue(view["unavailable"])       # 0/1 已转成 bool
        self.assertEqual(view["state"], STATE_COOLING)


class TestNormalizeUsage(unittest.TestCase):
    def test_explicit_request_id_becomes_dedupe_key(self):
        event = normalize_usage_record({"request_id": "req-1", "model": "gpt-5"}, 7)
        self.assertEqual(event["dedupe_key"], "id:req-1")
        self.assertEqual(event["node_id"], 7)

    def test_no_id_falls_back_to_hash_and_is_stable(self):
        raw = {"model": "gpt-5", "prompt_tokens": 1}
        first = normalize_usage_record(raw, 1)
        second = normalize_usage_record(dict(raw), 1)
        self.assertTrue(first["dedupe_key"].startswith("sha1:"))
        self.assertEqual(first["dedupe_key"], second["dedupe_key"])

    def test_aliases(self):
        event = normalize_usage_record({
            "model_name": "claude-sonnet-4", "prompt_tokens": 100, "completion_tokens": 50,
            "cache_read_input_tokens": 20, "thinking_tokens": 10, "req_id": None,
            "auth_index": "idx-9", "client_key": "sk-client", "status_code": 200,
            "duration_ms": 1500,
        }, 1)
        self.assertEqual(event["model"], "claude-sonnet-4")
        self.assertEqual(event["input_tokens"], 100)
        self.assertEqual(event["output_tokens"], 50)
        self.assertEqual(event["cached_tokens"], 20)
        self.assertEqual(event["reasoning_tokens"], 10)
        self.assertEqual(event["credential_index"], "idx-9")
        self.assertEqual(event["api_key"], "sk-client")
        self.assertEqual(event["latency_ms"], 1500)
        self.assertFalse(event["is_error"])

    def test_nested_usage_is_flattened(self):
        event = normalize_usage_record({
            "requestId": "req-nested", "model": "gpt-5-codex",
            "usage": {"prompt_tokens": 2000, "completion_tokens": 1000},
            "duration": 2.5, "status": "ok",
        }, 1)
        self.assertEqual(event["input_tokens"], 2000)
        self.assertEqual(event["output_tokens"], 1000)
        self.assertEqual(event["total_tokens"], 3000)
        self.assertEqual(event["latency_ms"], 2500)      # 秒 → 毫秒
        self.assertIn("usage", event["_nested_keys"])

    def test_error_detection(self):
        event = normalize_usage_record({"request_id": "e1", "status_code": 500,
                                        "error": "boom"}, 1)
        self.assertTrue(event["is_error"])
        self.assertIn("boom", event["error"])

    def test_control_frame_is_ignored(self):
        self.assertIsNone(normalize_usage_record({"support_refresh": True}, 1))
        self.assertIsNone(normalize_usage_record({"refresh": True}, 1))
        self.assertIsNone(normalize_usage_record("not-a-dict", 1))

    def test_unmapped_keys_are_reported(self):
        event = normalize_usage_record({"request_id": "x", "weird_field": 1, "model": "m"}, 1)
        self.assertIn("weird_field", event["_unmapped"])
        report = quality_report([event])
        self.assertEqual(report["total"], 1)
        self.assertIn(("weird_field", 1), report["unmapped_keys"])

    def test_quality_report_counts_missing(self):
        event = normalize_usage_record({"request_id": "only-id"}, 1)
        report = quality_report([event], )
        self.assertEqual(report["missing_model"], 1)
        self.assertEqual(report["missing_credential"], 1)
        self.assertEqual(report["missing_tokens"], 1)


class TestPricing(unittest.TestCase):
    def setUp(self):
        self.pricing = Pricing()

    def test_longest_prefix_match(self):
        self.assertIsNotNone(self.pricing.rates_for("claude-sonnet-4-20250514"))
        self.assertIsNotNone(self.pricing.rates_for("gpt-5-codex"))

    def test_estimate_uses_cache_rate(self):
        cost_plain = self.pricing.estimate("claude-sonnet-4", input_tokens=1_000_000)
        cost_cached = self.pricing.estimate("claude-sonnet-4", input_tokens=1_000_000,
                                            cached_tokens=1_000_000)
        self.assertAlmostEqual(cost_plain, 3.0, places=6)
        self.assertAlmostEqual(cost_cached, 0.3, places=6)

    def test_reasoning_counted_as_output(self):
        cost = self.pricing.estimate("claude-sonnet-4", output_tokens=500_000,
                                     reasoning_tokens=500_000)
        self.assertAlmostEqual(cost, 15.0, places=6)

    def test_unknown_model_is_zero(self):
        self.assertEqual(self.pricing.estimate("totally-unknown-model", input_tokens=1000), 0.0)

    def test_override_merges(self):
        pricing = Pricing({"gpt-5": {"input": 1.0, "output": 2.0}})
        self.assertEqual(pricing.rates_for("gpt-5")["input"], 1.0)
        self.assertEqual(pricing.rates_for("gpt-5")["output"], 2.0)


if __name__ == "__main__":
    unittest.main()
