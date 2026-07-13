from __future__ import annotations

import os
import tempfile
import unittest
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pricepilot.core.config import CONFIG
from pricepilot.core.database import (
    create_account,
    get_latest_user_consent,
    get_decision_log,
    get_telegram_approvals,
    init_db,
    record_user_consent,
    save_decision_log,
    save_telegram_link,
    upsert_calendar_price,
)
from pricepilot.services.property_service import create_property
from pricepilot.services.tenant_service import (
    api_auth_required,
    resolve_account_id_from_api_key,
)
from pricepilot.services.telegram_bot import (
    WEBHOOK_SECRET_HEADER,
    verify_webhook_secret,
    webhook_secret_required,
)
from pricepilot.dashboard import auth as dashboard_auth


class FakeRequest:
    def __init__(self, path: str, headers: dict | None = None, json_body: dict | None = None):
        self.url = SimpleNamespace(path=path)
        self.headers = headers or {}
        self.state = SimpleNamespace()
        self._json_body = json_body if json_body is not None else {}

    async def json(self):
        return self._json_body


async def ok_call_next(request):
    return SimpleNamespace(status_code=200, request=request)


class SecurityBasicsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = CONFIG["db_path"]
        CONFIG["db_path"] = str(Path(self._tmp.name) / "pricepilot_security_test.db")
        init_db()

    def tearDown(self):
        CONFIG["db_path"] = self._old_db_path
        self._tmp.cleanup()

    def test_api_auth_is_open_only_in_local_dev_without_keys(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(api_auth_required())
            self.assertEqual(resolve_account_id_from_api_key(None), 1)

    def test_api_auth_is_fail_closed_in_production_without_keys(self):
        with patch.dict(os.environ, {"PRICEPILOT_ENV": "production"}, clear=True):
            self.assertTrue(api_auth_required())
            self.assertIsNone(resolve_account_id_from_api_key(None))

    def test_dashboard_auth_fallback_is_closed_in_production(self):
        with patch.dict(os.environ, {"PRICEPILOT_ENV": "production"}, clear=True):
            self.assertFalse(dashboard_auth._local_auth_allowed())
            self.assertFalse(dashboard_auth._disabled_auth_allowed())

        with patch.dict(
            os.environ,
            {
                "PRICEPILOT_ENV": "production",
                "PRICEPILOT_ALLOW_LOCAL_AUTH": "1",
            },
            clear=True,
        ):
            self.assertTrue(dashboard_auth._local_auth_allowed())

    def test_supabase_auth_redirect_urls_have_clear_precedence(self):
        env = {
            "APP_BASE_URL": "https://app.example.test",
            "SUPABASE_AUTH_REDIRECT_URL": "https://auth.example.test/",
            "SUPABASE_PASSWORD_RESET_REDIRECT_URL": "https://reset.example.test/",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(dashboard_auth._auth_redirect_url(), "https://auth.example.test")
            self.assertEqual(dashboard_auth._password_reset_redirect_url(), "https://reset.example.test")

    def test_api_keys_resolve_account_server_side(self):
        env = {"PRICEPILOT_API_KEYS_JSON": '{"key-a": 2, "key-b": 7}'}
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(api_auth_required())
            self.assertEqual(resolve_account_id_from_api_key("key-a"), 2)
            self.assertEqual(resolve_account_id_from_api_key("key-b"), 7)
            self.assertIsNone(resolve_account_id_from_api_key("bad-key"))

    def test_telegram_webhook_secret_rules(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(webhook_secret_required())
            self.assertTrue(verify_webhook_secret(None))

        with patch.dict(os.environ, {"PRICEPILOT_ENV": "production"}, clear=True):
            self.assertTrue(webhook_secret_required())
            self.assertFalse(verify_webhook_secret(None))

        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "secret-123"}, clear=True):
            self.assertTrue(webhook_secret_required())
            self.assertTrue(verify_webhook_secret("secret-123"))
            self.assertFalse(verify_webhook_secret("wrong"))

    def test_supabase_schema_uses_authenticated_rls(self):
        schema_path = Path(__file__).resolve().parents[1] / "supabase" / "schema.sql"
        sql = schema_path.read_text(encoding="utf-8").lower()

        self.assertIn("account_members", sql)
        self.assertIn("auth.uid()", sql)
        self.assertIn("enable row level security", sql)
        for table in (
            "user_consents",
            "decision_log",
            "price_calendar",
            "telegram_links",
            "telegram_approvals",
            "property_integrations",
            "operation_runs",
            "audit_events",
            "notification_preferences",
            "notification_log",
        ):
            self.assertIn(f"public.{table}", sql)
        self.assertNotIn("to anon", sql)
        self.assertNotIn("using (true)", sql)

    def test_user_consent_is_recorded_locally(self):
        from pricepilot.services.account_service import create_account_owner

        result = create_account_owner(
            email="consent-owner@example.test",
            password_hash="test-hash",
            account_name="Consent Host",
            plan="free",
        )
        consent = record_user_consent(
            result["user"]["id"],
            result["account"]["id"],
            terms_accepted=True,
            privacy_accepted=True,
            marketing_accepted=False,
            source="unit_test",
        )
        latest = get_latest_user_consent(result["user"]["id"])

        self.assertTrue(consent["terms_accepted"])
        self.assertTrue(consent["privacy_accepted"])
        self.assertFalse(consent["marketing_accepted"])
        self.assertEqual(latest["source"], "unit_test")
        self.assertEqual(consent["terms_version"], dashboard_auth.TERMS_VERSION)
        self.assertEqual(consent["privacy_version"], dashboard_auth.PRIVACY_VERSION)

    def test_public_legal_documents_are_versioned_and_accessible(self):
        self.assertIn("terms", dashboard_auth.PUBLIC_VIEWS)
        self.assertIn("privacy", dashboard_auth.PUBLIC_VIEWS)
        self.assertIn("cookies", dashboard_auth.PUBLIC_VIEWS)
        self.assertEqual(dashboard_auth.TERMS_VERSION, "2026-07-13")
        self.assertEqual(dashboard_auth.PRIVACY_VERSION, "2026-07-13")
        self.assertEqual(dashboard_auth.COOKIES_VERSION, "2026-07-13")

        for key in ("terms", "privacy", "cookies"):
            doc = dashboard_auth.LEGAL_DOCUMENTS[key]
            self.assertTrue(doc["title"])
            self.assertTrue(doc["version"])
            self.assertGreaterEqual(len(doc["sections"]), 4)

    def test_supabase_migration_dry_run_counts_account_data(self):
        from pricepilot.services.supabase_migration import dry_run_sqlite_account_migration

        account = create_account("Migration Host", plan="plus", billing_status="active")
        prop = create_property({
            "account_id": account["id"],
            "name": "Migration Apt",
            "platform": "airbnb",
            "listing_url": "",
            "listing_id": "migration-apt",
            "city": "Lucca",
            "min_price": 80.0,
            "max_price": 220.0,
            "plan": "plus",
            "sync_mode": "approval",
        })
        save_decision_log({
            "account_id": account["id"],
            "property_id": prop["id"],
            "old_price": 100.0,
            "new_price": 120.0,
            "market_avg": 115.0,
            "occupancy": 0.70,
            "decision": "PENDING_APPROVAL",
            "mode": "approval",
            "applied": 0,
            "date": "2026-06-23",
        })
        upsert_calendar_price({
            "account_id": account["id"],
            "property_id": prop["id"],
            "date": "2026-06-23",
            "current_price": 100.0,
            "recommended_price": 120.0,
            "status": "pending_approval",
        })
        save_telegram_link({
            "property_id": prop["id"],
            "token": "migration_test",
            "chat_id": 123,
            "telegram_username": "host",
            "active": 1,
        })

        dry_run = dry_run_sqlite_account_migration(account["id"])

        self.assertEqual(dry_run["account_id"], account["id"])
        self.assertEqual(dry_run["tables"]["properties"], 1)
        self.assertEqual(dry_run["tables"]["pricing_rules"], 1)
        self.assertEqual(dry_run["tables"]["decision_log"], 1)
        self.assertEqual(dry_run["tables"]["price_calendar"], 1)
        self.assertEqual(dry_run["tables"]["telegram_links"], 1)

    def test_supabase_account_sync_requires_auth_or_service_role_context(self):
        import pricepilot.services.supabase_repository as repo

        account = create_account("No Context Host", plan="free", billing_status="active")
        prop = {
            "id": 77,
            "account_id": account["id"],
            "name": "No Context Apt",
            "platform": "airbnb",
            "listing_url": "",
            "listing_id": "no-context",
            "city": "Lucca",
            "min_price": 70.0,
            "max_price": 180.0,
            "plan": "free",
            "sync_mode": "advisory",
        }

        with patch.object(repo, "get_supabase_account_client", return_value=None):
            self.assertFalse(repo.has_supabase_write_context())
            self.assertIsNone(repo.sync_property_to_supabase(prop))
            self.assertIsNone(repo.sync_pricing_rule_to_supabase(prop))
            backfill = repo.backfill_account_properties_to_supabase(account["id"], [prop])

        self.assertEqual(backfill["skipped"], 1)
        self.assertEqual(backfill["properties"], 0)
        self.assertEqual(backfill["pricing_rules"], 0)

    def test_supabase_migration_requires_write_context(self):
        from pricepilot.services import supabase_migration

        with patch.object(supabase_migration, "has_supabase_write_context", return_value=False):
            result = supabase_migration.migrate_sqlite_account_to_supabase(1)

        self.assertFalse(result["ok"])
        self.assertIn("Manca un contesto Supabase autenticato", result["error"])

    def test_api_private_routes_block_without_key_in_production(self):
        from pricepilot.api import server

        with patch.dict(os.environ, {"PRICEPILOT_ENV": "production"}, clear=True):
            request = FakeRequest("/properties")
            response = asyncio.run(server.api_key_guard(request, ok_call_next))
        self.assertEqual(response.status_code, 401)

    def test_api_readiness_is_public_but_reports_missing_production_config(self):
        from pricepilot.api import server

        with patch.dict(os.environ, {"PRICEPILOT_ENV": "production"}, clear=True):
            request = FakeRequest("/ready")
            response = asyncio.run(server.api_key_guard(request, ok_call_next))
            readiness = server.api_readiness()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(readiness["ok"])
        self.assertFalse(readiness["checks"]["api_base_url"]["ok"])
        self.assertFalse(readiness["checks"]["api_auth"]["ok"])
        self.assertFalse(readiness["checks"]["supabase"]["ok"])
        self.assertFalse(readiness["checks"]["telegram_webhook_secret"]["ok"])
        self.assertFalse(readiness["checks"]["stripe_webhook_secret"]["ok"])
        self.assertFalse(readiness["checks"]["billing_provider"]["ok"])
        self.assertFalse(readiness["checks"]["data_providers"]["ok"])

    def test_telegram_webhook_auto_registration_is_explicit_and_uses_api_url(self):
        from pricepilot.api import server

        calls = []
        old_set_webhook = server.set_webhook
        old_is_configured = server.is_configured
        server.set_webhook = lambda base_url: calls.append(base_url) or {"ok": True}
        server.is_configured = lambda: True
        try:
            with patch.dict(os.environ, {"APP_BASE_URL": "https://dashboard.example.test"}, clear=True):
                server._register_webhook_if_needed()
            self.assertEqual(calls, [])

            env = {
                "PRICEPILOT_AUTO_REGISTER_TELEGRAM_WEBHOOK": "1",
                "PRICEPILOT_API_BASE_URL": "https://api.example.test/",
                "TELEGRAM_WEBHOOK_SECRET": "secret-123",
            }
            with patch.dict(os.environ, env, clear=True):
                server._register_webhook_if_needed()
            self.assertEqual(calls, ["https://api.example.test"])
        finally:
            server.set_webhook = old_set_webhook
            server.is_configured = old_is_configured

    def test_telegram_webhook_requires_secret_in_production(self):
        from pricepilot.api import server

        old_handler = server.tg_process_webhook
        server.tg_process_webhook = lambda update: None
        try:
            with patch.dict(os.environ, {"PRICEPILOT_ENV": "production"}, clear=True):
                with self.assertRaises(server.HTTPException) as missing:
                    asyncio.run(server.telegram_webhook(FakeRequest("/telegram/webhook")))
            self.assertEqual(missing.exception.status_code, 503)

            env = {"PRICEPILOT_ENV": "production", "TELEGRAM_WEBHOOK_SECRET": "secret-123"}
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaises(server.HTTPException) as wrong:
                    asyncio.run(server.telegram_webhook(
                        FakeRequest("/telegram/webhook", headers={WEBHOOK_SECRET_HEADER: "wrong"})
                    ))
                ok = asyncio.run(server.telegram_webhook(
                    FakeRequest("/telegram/webhook", headers={WEBHOOK_SECRET_HEADER: "secret-123"})
                ))
            self.assertEqual(wrong.exception.status_code, 401)
            self.assertEqual(ok, {"ok": True})
        finally:
            server.tg_process_webhook = old_handler

    def test_telegram_callback_can_only_approve_linked_property_chat(self):
        import pricepilot.services.telegram_bot as telegram_bot

        account = create_account("Telegram Security", plan="plus", billing_status="active")
        prop = create_property({
            "account_id": account["id"],
            "name": "Secure Apt",
            "platform": "airbnb",
            "listing_url": "",
            "listing_id": "secure-apt",
            "city": "Lucca",
            "min_price": 70.0,
            "max_price": 180.0,
            "plan": "plus",
            "sync_mode": "approval",
        })
        log_id = save_decision_log({
            "account_id": account["id"],
            "property_id": prop["id"],
            "old_price": 100.0,
            "new_price": 120.0,
            "market_avg": 115.0,
            "occupancy": 0.70,
            "decision": "PENDING_APPROVAL",
            "mode": "approval",
            "applied": 0,
            "date": "2026-05-18",
        })
        save_telegram_link({
            "property_id": prop["id"],
            "token": "connect_test",
            "chat_id": 111,
            "telegram_username": "owner",
            "active": 1,
        })

        old_answer = telegram_bot.answer_callback_query
        old_edit = telegram_bot.edit_message_text
        telegram_bot.answer_callback_query = lambda *args, **kwargs: {"ok": True}
        telegram_bot.edit_message_text = lambda *args, **kwargs: {"ok": True}
        try:
            telegram_bot.process_webhook({
                "callback_query": {
                    "id": "bad-chat",
                    "data": f"approve_{log_id}",
                    "message": {
                        "chat": {"id": 999},
                        "message_id": 10,
                        "text": "Decisione",
                    },
                }
            })
            after_bad = get_decision_log(account_id=account["id"])[0]
            self.assertNotIn("[APPROVED", after_bad["decision"])
            self.assertEqual(get_telegram_approvals(account_id=account["id"]), [])

            telegram_bot.process_webhook({
                "callback_query": {
                    "id": "good-chat",
                    "data": f"approve_{log_id}",
                    "message": {
                        "chat": {"id": 111},
                        "message_id": 11,
                        "text": "Decisione",
                    },
                }
            })
            after_good = get_decision_log(account_id=account["id"])[0]
            self.assertIn("[APPROVED_PENDING_MANUAL_SYNC]", after_good["decision"])
            history = get_telegram_approvals(account_id=account["id"])
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["action"], "approve")
            self.assertEqual(history[0]["status"], "approved_pending_manual_sync")
            self.assertEqual(history[0]["source"], "telegram")
        finally:
            telegram_bot.answer_callback_query = old_answer
            telegram_bot.edit_message_text = old_edit


if __name__ == "__main__":
    unittest.main()
