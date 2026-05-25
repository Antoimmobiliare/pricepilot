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
    get_decision_log,
    init_db,
    save_decision_log,
    save_telegram_link,
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

    def test_api_private_routes_block_without_key_in_production(self):
        from pricepilot.api import server

        with patch.dict(os.environ, {"PRICEPILOT_ENV": "production"}, clear=True):
            request = FakeRequest("/properties")
            response = asyncio.run(server.api_key_guard(request, ok_call_next))
        self.assertEqual(response.status_code, 401)

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
        finally:
            telegram_bot.answer_callback_query = old_answer
            telegram_bot.edit_message_text = old_edit


if __name__ == "__main__":
    unittest.main()
