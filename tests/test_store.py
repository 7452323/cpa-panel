"""存储层测试：快照差异、用量幂等、聚合、Key 同步、清理。"""

import os
import tempfile
import time
import unittest

from cpapanel.models import normalize_auth_file, normalize_usage_record
from cpapanel.pricing import Pricing
from cpapanel.store import Store
from cpapanel.util import day_of, now_ts


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cpa-panel-test-")
        self.store = Store(os.path.join(self.tmpdir, "panel.db"))
        self.node_id = self.store.add_node("mock", "http://127.0.0.1:8317", "sk-key", "v8")

    def tearDown(self):
        self.store.close()

    def cred(self, **overrides):
        entry = {
            "id": "c1", "auth_index": "idx-1", "name": "codex-1.json", "provider": "codex",
            "status": "active", "status_message": "", "disabled": False, "unavailable": False,
            "success": 1, "failed": 0, "email": "a@example.com",
            "quota": {"observed_at": now_ts(), "signals": {}},
            "id_token": {"plan_type": "plus"},
        }
        entry.update(overrides)
        return normalize_auth_file(entry)

    def event(self, **overrides):
        raw = {"request_id": "req-1", "model": "gpt-5-codex", "provider": "codex",
               "auth_index": "idx-1", "api_key": "sk-client-abcdefghijklmnop", "status_code": 200,
               "prompt_tokens": 100, "completion_tokens": 50}
        raw.update(overrides)
        item = normalize_usage_record(raw, self.node_id)
        item["cost_usd"] = Pricing().estimate(item["model"], item["input_tokens"],
                                             item["output_tokens"])
        return item


class TestCredentials(StoreTestCase):
    def test_new_credential_is_added_with_event(self):
        result = self.store.sync_credentials(self.node_id, [self.cred()])
        self.assertEqual(result["added"], 1)
        events = self.store.q("SELECT * FROM credential_events")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "added")

    def test_unchanged_sync_produces_no_change(self):
        self.store.sync_credentials(self.node_id, [self.cred()])
        before = len(self.store.q("SELECT * FROM credential_events"))
        result = self.store.sync_credentials(self.node_id, [self.cred()])
        self.assertEqual(result["changed"], 0)
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(len(self.store.q("SELECT * FROM credential_events")), before)

    def test_status_change_is_recorded(self):
        self.store.sync_credentials(self.node_id, [self.cred()])
        result = self.store.sync_credentials(
            self.node_id, [self.cred(status="error", status_message="token expired")])
        self.assertEqual(result["changed"], 1)
        kinds = {c["kind"] for c in result["changes"]}
        self.assertIn("state_changed", kinds)
        fields = {c.get("field") for c in result["changes"]}
        self.assertIn("status", fields)

    def test_vanished_credential_marked_absent(self):
        self.store.sync_credentials(self.node_id, [self.cred()])
        result = self.store.sync_credentials(self.node_id, [])
        self.assertEqual(result["removed"], 1)
        row = self.store.q1("SELECT present FROM credentials")
        self.assertEqual(row["present"], 0)
        self.assertEqual(self.store.credential_counts(self.node_id)["total"], 0)

    def test_counts_and_breakdown(self):
        self.store.sync_credentials(self.node_id, [
            self.cred(name="a.json", auth_index="i1"),
            self.cred(name="b.json", auth_index="i2", status="error",
                      status_message="token expired"),
            self.cred(name="c.json", auth_index="i3", provider="claude", disabled=True),
            self.cred(name="d.json", auth_index="i4", unavailable=True,
                      next_retry_after=now_ts() + 300),
        ])
        counts = self.store.credential_counts(self.node_id)
        self.assertEqual(counts["total"], 4)
        self.assertEqual(counts["disabled"], 1)
        self.assertEqual(counts["cooling"], 1)
        self.assertEqual(counts["erroring"], 1)
        self.assertEqual(counts["active"], 2)
        providers = {p["provider"]: p for p in self.store.provider_breakdown(self.node_id)}
        self.assertEqual(providers["codex"]["total"], 3)

    def test_standby_flag_roundtrip(self):
        self.store.sync_credentials(self.node_id, [self.cred()])
        cred_id = self.store.q1("SELECT id FROM credentials")["id"]
        self.store.set_credential_standby(int(cred_id), True)
        self.assertEqual(len(self.store.list_credentials(self.node_id, standby=True)), 1)
        self.store.set_credential_standby(int(cred_id), False)
        self.assertEqual(len(self.store.list_credentials(self.node_id, standby=True)), 0)

    def test_search_filters(self):
        self.store.sync_credentials(self.node_id, [
            self.cred(name="alpha.json", auth_index="i1", email="alpha@x.com"),
            self.cred(name="beta.json", auth_index="i2", email="beta@y.com"),
        ])
        self.assertEqual(len(self.store.list_credentials(self.node_id, keyword="alpha")), 1)
        self.assertEqual(len(self.store.list_credentials(self.node_id, provider="codex")), 2)
        self.assertEqual(len(self.store.list_credentials(self.node_id, provider="claude")), 0)


class TestUsage(StoreTestCase):
    def test_dedupe_prevents_double_counting(self):
        event = self.event()
        self.assertTrue(self.store.insert_usage_event(self.node_id, event))
        self.assertFalse(self.store.insert_usage_event(self.node_id, dict(event)))
        row = self.store.q1("SELECT COUNT(*) AS n FROM usage_events")
        self.assertEqual(row["n"], 1)

    def test_aggregate_and_summary(self):
        event = self.event()
        self.store.insert_usage_event(self.node_id, event)
        self.store.aggregate_event(dict(event, _node_id=self.node_id))
        summary = self.store.usage_summary(1, self.node_id)
        self.assertEqual(summary["requests"], 1)
        self.assertEqual(summary["input_tokens"], 100)
        self.assertEqual(summary["output_tokens"], 50)
        self.assertEqual(summary["total_tokens"], 150)
        self.assertGreater(summary["cost_usd"], 0)

    def test_error_rate_and_series(self):
        ok = self.event(request_id="ok-1")
        bad = self.event(request_id="bad-1", status_code=500, error="boom")
        for event in (ok, bad):
            self.store.insert_usage_event(self.node_id, event)
            self.store.aggregate_event(dict(event, _node_id=self.node_id))
        summary = self.store.usage_summary(1, self.node_id)
        self.assertEqual(summary["requests"], 2)
        self.assertEqual(summary["errors"], 1)
        self.assertAlmostEqual(summary["error_rate"], 0.5)
        series = self.store.usage_series(2, self.node_id)
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0]["day"], day_of(now_ts()))

    def test_group_by_model_and_credential(self):
        for i in range(3):
            event = self.event(request_id=f"r{i}")
            self.store.insert_usage_event(self.node_id, event)
            self.store.aggregate_event(dict(event, _node_id=self.node_id))
        by_model = self.store.usage_by_model(1, 10, self.node_id)
        self.assertEqual(by_model[0]["model"], "gpt-5-codex")
        self.assertEqual(by_model[0]["requests"], 3)
        by_cred = self.store.usage_by_credential(1, 10, self.node_id)
        self.assertEqual(by_cred[0]["credential_index"], "idx-1")

    def test_event_listing_and_apikey_masking(self):
        event = self.event()
        self.store.insert_usage_event(self.node_id, event)
        rows = self.store.list_usage_events(self.node_id, limit=10)
        self.assertEqual(len(rows), 1)
        masked = rows[0]["api_key_masked"]
        self.assertNotEqual(masked, rows[0]["api_key"])
        self.assertIn("…", masked)
        self.assertTrue(masked.startswith("sk-cli"))
        self.assertEqual(len(self.store.list_usage_events(self.node_id, errors_only=True)), 0)

    def test_retention_prune(self):
        event = self.event()
        event["ts"] = now_ts() - 400 * 86400
        event["day"] = day_of(event["ts"])
        self.store.insert_usage_event(self.node_id, event)
        deleted = self.store.prune(usage_days=180)
        self.assertEqual(deleted["usage_events"], 1)


class TestApiKeys(StoreTestCase):
    def test_sync_add_then_remove(self):
        result = self.store.sync_api_keys(self.node_id, ["sk-a", "sk-b"])
        self.assertEqual(result["added"], 2)
        self.assertEqual(len(self.store.list_api_keys(self.node_id)), 2)
        result = self.store.sync_api_keys(self.node_id, ["sk-a"])
        self.assertEqual(result["removed"], 1)
        self.assertEqual(len(self.store.list_api_keys(self.node_id)), 1)

    def test_key_usage_attached(self):
        self.store.sync_api_keys(self.node_id, ["sk-a"])
        self.store.upsert_key_usage(self.node_id, [{"api_key": "sk-a", "requests": 12,
                                                    "input_tokens": 5, "output_tokens": 6,
                                                    "cost_usd": 0.5}])
        key = self.store.list_api_keys(self.node_id)[0]
        self.assertEqual(key["usage"]["requests"], 12)
        self.assertEqual(key["usage"]["cost_usd"], 0.5)
        self.assertNotIn("raw", key)


class TestAccountsAndAudit(StoreTestCase):
    def test_user_session_lifecycle(self):
        from cpapanel.security import hash_password, verify_password
        uid = self.store.create_user("admin", hash_password("s3cret-pass"))
        user = self.store.get_user("admin")
        self.assertTrue(verify_password("s3cret-pass", user["password_hash"]))
        self.assertFalse(verify_password("wrong", user["password_hash"]))
        self.store.create_session("tok", uid, 3600, "127.0.0.1", "ua")
        self.assertIsNotNone(self.store.get_session("tok"))
        self.store.delete_session("tok")
        self.assertIsNone(self.store.get_session("tok"))

    def test_expired_session_is_dropped(self):
        uid = self.store.create_user("admin", "x")
        self.store.create_session("old", uid, -1, "", "")
        self.assertIsNone(self.store.get_session("old"))

    def test_audit_and_inspection_records(self):
        self.store.audit("test.action", actor="unit", target="x")
        self.assertEqual(len(self.store.list_audit(10)), 1)
        iid = self.store.start_inspection(self.node_id, "dry_run")
        self.store.add_action(iid, self.node_id, "a.json", "disable", "reason", "ok", "")
        self.store.finish_inspection(iid, scanned=3, planned=1, summary_json={"counts": {"a": 1}})
        inspections = self.store.list_inspections(self.node_id)
        self.assertEqual(inspections[0]["scanned"], 3)
        self.assertEqual(inspections[0]["summary"]["counts"]["a"], 1)
        self.assertEqual(len(self.store.list_actions(self.node_id)), 1)

    def test_stats(self):
        self.store.sync_credentials(self.node_id, [self.cred()])
        event = self.event()
        self.store.insert_usage_event(self.node_id, event)
        stats = self.store.stats()
        self.assertEqual(stats["credentials"], 1)
        self.assertEqual(stats["usage_events"], 1)
        self.assertGreater(stats["database_bytes"], 0)


class TestNodeManagement(StoreTestCase):
    def test_bootstrap_is_idempotent(self):
        store = Store(os.path.join(self.tmpdir, "second.db"))
        try:
            items = [{"name": "n1", "base_url": "http://a:8317", "management_key": "k",
                      "api_prefix": "auto"}]
            self.assertEqual(len(store.ensure_bootstrap_nodes(items)), 1)
            self.assertEqual(len(store.ensure_bootstrap_nodes(items)), 0)
            self.assertEqual(len(store.list_nodes()), 1)
        finally:
            store.close()

    def test_delete_node_cascades_but_keeps_usage(self):
        self.store.sync_credentials(self.node_id, [self.cred()])
        event = self.event()
        self.store.insert_usage_event(self.node_id, event)
        self.store.delete_node(self.node_id)
        self.assertEqual(len(self.store.list_nodes()), 0)
        self.assertEqual(len(self.store.q("SELECT * FROM credentials")), 0)
        self.assertEqual(len(self.store.q("SELECT * FROM usage_events")), 1)


if __name__ == "__main__":
    unittest.main()
