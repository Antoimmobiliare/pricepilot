"""Tenant-scope and secret-redaction checks for the operational API."""
from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import unittest
from unittest.mock import patch

from pricepilot.core import database as db
from pricepilot.core.config import CONFIG
from pricepilot.services.property_service import create_property


class OperationalApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            "PRICEPILOT_TESTING": "1",
            "PRICEPILOT_DATABASE_BACKEND": "sqlite",
            "SUPABASE_URL": "",
            "SUPABASE_ANON_KEY": "",
            "SUPABASE_SERVICE_ROLE_KEY": "",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        old_path = CONFIG["db_path"]
        CONFIG["db_path"] = str(Path(self.tmp.name) / "api.db")
        self.addCleanup(lambda: CONFIG.update(db_path=old_path))
        db.init_db()
        self.account = db.create_account("API owner", plan="plus")["id"]
        self.other = db.create_account("Other owner", plan="plus")["id"]
        self.prop = create_property({
            "account_id": self.account, "name": "Luma fixture",
            "platform": "airbnb", "min_price": 60, "max_price": 300,
            "sync_mode": "approval",
        })

    @staticmethod
    def request(account_id):
        return SimpleNamespace(state=SimpleNamespace(account_id=account_id))

    def test_policy_and_connection_are_scoped_and_secrets_are_redacted(self):
        from pricepilot.api import server
        request = self.request(self.account)
        policy = server.CalendarPolicyUpdate(
            enabled=True,
            reference_price=110,
            weekend_multiplier=1.1,
            break_even=65,
            lead_time_bands=[server.LeadTimeBand(
                through_days=366, low_occupancy=.35, high_occupancy=.8,
                low_multiplier=.9, high_multiplier=1.12,
            )],
        )
        saved = server.api_save_calendar_policy(self.prop["id"], policy, request)
        self.assertEqual(saved["account_id"], self.account)
        self.assertTrue(server.api_get_calendar_policy(self.prop["id"], request)["enabled"])

        connection = server.Beds24ConnectionUpdate(
            enabled=True, beds24_property_id=10, room_id=20, price_slot=1,
            token_env="BEDS24_LUMA_TOKEN", refresh_token_env="BEDS24_LUMA_REFRESH",
        )
        with patch.dict(os.environ, {
            "BEDS24_LUMA_TOKEN": "do-not-return-token",
            "BEDS24_LUMA_REFRESH": "do-not-return-refresh",
        }):
            server.api_save_channel_connection(self.prop["id"], connection, request)
            response = server.api_get_channel_connection(self.prop["id"], request)
        self.assertTrue(response["token_configured"])
        self.assertTrue(response["refresh_token_configured"])
        self.assertNotIn("do-not-return", repr(response))

        for fn in (server.api_get_calendar_policy, server.api_get_channel_connection):
            with self.assertRaises(server.HTTPException) as error:
                fn(self.prop["id"], self.request(self.other))
            self.assertEqual(error.exception.status_code, 404)

    def test_delete_cannot_cross_account(self):
        from pricepilot.api import server
        with self.assertRaises(server.HTTPException) as error:
            server.api_delete_property(self.prop["id"], self.request(self.other))
        self.assertEqual(error.exception.status_code, 404)
        self.assertIsNotNone(db.get_property(self.prop["id"], account_id=self.account))

    def test_reject_is_compare_and_set_and_account_scoped(self):
        from pricepilot.api import server
        decision_id = db.save_decision_log({
            "account_id": self.account, "property_id": self.prop["id"],
            "old_price": 100, "new_price": 90, "market_avg": 0,
            "occupancy": .25, "decision": "PENDING_APPROVAL",
            "mode": "approval", "applied": 0, "date": "2026-10-02",
        })
        body = server.ApprovalRequest(log_id=decision_id)
        with self.assertRaises(server.HTTPException) as error:
            server.api_reject(body, self.request(self.other))
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(server.api_reject(body, self.request(self.account))["status"], "rejected")
        with self.assertRaises(server.HTTPException) as error:
            server.api_reject(body, self.request(self.account))
        self.assertEqual(error.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
