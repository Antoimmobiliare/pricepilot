from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pricepilot.core.config import CONFIG
from pricepilot.core.database import create_account, get_account, init_db
from pricepilot.dashboard.auth import _account_plan_for_signup
from pricepilot.providers.demo import LocalBillingProvider
from pricepilot.providers.stripe_billing import StripeBillingProvider
from pricepilot.services.account_service import update_account_profile


class BillingProviderTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = CONFIG["db_path"]
        CONFIG["db_path"] = str(Path(self._tmp.name) / "pricepilot_billing_test.db")
        init_db()

    def tearDown(self):
        CONFIG["db_path"] = self._old_db_path
        self._tmp.cleanup()

    def test_local_billing_is_not_checkout_configured(self):
        provider = LocalBillingProvider()

        result = provider.create_checkout_session(account_id=1, plan="plus")

        self.assertFalse(provider.is_billing_configured())
        self.assertFalse(result.ok)
        self.assertEqual(result.provider, "local_billing")

    def test_stripe_provider_creates_checkout_payload_when_configured(self):
        env = {
            "STRIPE_SECRET_KEY": "sk_test_123",
            "STRIPE_PRICE_PLUS": "price_plus",
            "STRIPE_PRICE_PRO": "price_pro",
            "APP_BASE_URL": "https://pricepilot.test",
        }
        captured = {}

        class FakeSession:
            @staticmethod
            def create(**payload):
                captured.update(payload)
                return SimpleNamespace(id="cs_test_123", url="https://checkout.stripe.test/session")

        fake_stripe = SimpleNamespace(
            api_key="",
            checkout=SimpleNamespace(Session=FakeSession),
        )

        with patch.dict(os.environ, env, clear=True):
            account = create_account("Billing Host", plan="free", billing_status="dev")
            provider = StripeBillingProvider()
            provider._stripe_module = lambda: fake_stripe

            result = provider.create_checkout_session(account_id=int(account["id"]), plan="plus")

        self.assertTrue(provider.is_billing_configured())
        self.assertTrue(result.ok)
        self.assertEqual(result.url, "https://checkout.stripe.test/session")
        self.assertEqual(captured["mode"], "subscription")
        self.assertEqual(captured["line_items"][0]["price"], "price_plus")
        self.assertEqual(captured["metadata"]["plan"], "plus")
        self.assertEqual(captured["client_reference_id"], str(account["id"]))

    def test_account_profile_update_cannot_change_billing_fields(self):
        account = create_account("Manual Billing Host", plan="free", billing_status="dev")

        updated = update_account_profile(account["id"], {
            "name": "Renamed Host",
            "plan": "pro",
            "billing_status": "active",
            "stripe_customer_id": "cus_manual",
            "stripe_subscription_id": "sub_manual",
        })

        self.assertEqual(updated["name"], "Renamed Host")
        self.assertEqual(updated["plan"], "free")
        self.assertEqual(updated["billing_status"], "dev")
        self.assertEqual(updated.get("stripe_customer_id", ""), "")

    def test_stripe_checkout_webhook_activates_paid_plan(self):
        env = {
            "STRIPE_SECRET_KEY": "sk_test_123",
            "STRIPE_PRICE_PLUS": "price_plus",
            "STRIPE_PRICE_PRO": "price_pro",
        }
        account = create_account("Webhook Host", plan="free", billing_status="dev")
        event = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": str(account["id"]),
                    "metadata": {"account_id": str(account["id"]), "plan": "plus"},
                    "customer": "cus_123",
                    "subscription": "sub_123",
                }
            },
        }

        with patch.dict(os.environ, env, clear=True):
            provider = StripeBillingProvider()
            provider._stripe_module = lambda: SimpleNamespace()
            result = provider.process_webhook(payload=json.dumps(event).encode("utf-8"))

        updated = get_account(account["id"])
        self.assertTrue(result.ok)
        self.assertEqual(updated["plan"], "plus")
        self.assertEqual(updated["billing_status"], "active")
        self.assertEqual(updated["stripe_customer_id"], "cus_123")
        self.assertEqual(updated["stripe_subscription_id"], "sub_123")

    def test_stripe_subscription_deleted_downgrades_to_free(self):
        env = {
            "STRIPE_SECRET_KEY": "sk_test_123",
            "STRIPE_PRICE_PLUS": "price_plus",
            "STRIPE_PRICE_PRO": "price_pro",
        }
        account = create_account("Canceled Host", plan="pro", billing_status="active")
        event = {
            "type": "customer.subscription.deleted",
            "data": {
                "object": {
                    "id": "sub_456",
                    "status": "canceled",
                    "metadata": {"account_id": str(account["id"]), "plan": "pro"},
                    "customer": "cus_456",
                }
            },
        }

        with patch.dict(os.environ, env, clear=True):
            provider = StripeBillingProvider()
            provider._stripe_module = lambda: SimpleNamespace()
            result = provider.process_webhook(payload=json.dumps(event).encode("utf-8"))

        updated = get_account(account["id"])
        self.assertTrue(result.ok)
        self.assertEqual(updated["plan"], "free")
        self.assertEqual(updated["billing_status"], "canceled")
        self.assertEqual(updated["stripe_customer_id"], "cus_456")
        self.assertEqual(updated["stripe_subscription_id"], "sub_456")

    def test_stripe_webhook_requires_signature_secret_in_production(self):
        env = {
            "PRICEPILOT_ENV": "production",
            "STRIPE_SECRET_KEY": "sk_test_123",
            "STRIPE_PRICE_PLUS": "price_plus",
            "STRIPE_PRICE_PRO": "price_pro",
        }
        event = {"type": "checkout.session.completed", "data": {"object": {}}}

        with patch.dict(os.environ, env, clear=True):
            provider = StripeBillingProvider()
            provider._stripe_module = lambda: SimpleNamespace()
            result = provider.process_webhook(payload=json.dumps(event).encode("utf-8"))

        self.assertFalse(result.ok)
        self.assertIn("STRIPE_WEBHOOK_SECRET", result.error)

    def test_production_signup_does_not_grant_paid_plan_without_checkout(self):
        with patch.dict(os.environ, {"PRICEPILOT_ENV": "production"}, clear=True):
            self.assertEqual(_account_plan_for_signup("plus"), "free")
            self.assertEqual(_account_plan_for_signup("pro"), "free")
            self.assertEqual(_account_plan_for_signup("free"), "free")

    def test_local_signup_can_still_use_paid_plans_for_testing(self):
        with patch.dict(os.environ, {"PRICEPILOT_ENV": "development"}, clear=True):
            self.assertEqual(_account_plan_for_signup("plus"), "plus")
            self.assertEqual(_account_plan_for_signup("pro"), "pro")


if __name__ == "__main__":
    unittest.main()
