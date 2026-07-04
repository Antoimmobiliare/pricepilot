from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from pricepilot.integrations.channel_manager import get_channel_manager
from pricepilot.integrations.smoobu import SmoobuAdapter


class SmoobuAdapterTests(unittest.TestCase):
    def test_stub_update_is_safe_without_credentials(self):
        adapter = SmoobuAdapter(listing_id="", api_token="", api_key="", api_secret="")

        result = adapter.update_price(150.0, date(2026, 6, 26))

        self.assertTrue(result.ok)
        self.assertEqual(result.platform, "smoobu")
        self.assertEqual(result.raw.get("stub"), True)

    def test_hmac_auth_uses_current_smoobu_headers(self):
        adapter = SmoobuAdapter(
            listing_id="123",
            api_token="",
            api_key="consumer-key",
            api_secret="consumer-secret",
        )

        headers = adapter._headers(
            "POST",
            "/api/rates",
            {"apartments": [123]},
            "",
        )

        self.assertEqual(headers["X-API-Key"], "consumer-key")
        self.assertIn("X-Timestamp", headers)
        self.assertIn("X-Nonce", headers)
        self.assertIn("X-Signature", headers)
        self.assertNotIn("Api-Consumer-Key", headers)
        self.assertEqual(adapter.auth_mode(), "hmac")

    def test_update_price_builds_smoobu_rates_payload_without_network(self):
        adapter = SmoobuAdapter(
            listing_id="123",
            api_token="",
            api_key="consumer-key",
            api_secret="consumer-secret",
        )
        calls = []

        def fake_call(method, path, body=None, query=None, timeout=15):
            calls.append({"method": method, "path": path, "body": body, "query": query})
            return {"ok": True, "data": {"success": True}}

        adapter._call = fake_call
        result = adapter.update_price(172.0, date(2026, 6, 26), min_nights=2)

        self.assertTrue(result.ok)
        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["path"], "/api/rates")
        self.assertEqual(calls[0]["body"]["apartments"], [123])
        self.assertEqual(calls[0]["body"]["operations"][0]["dates"], ["2026-06-26"])
        self.assertEqual(calls[0]["body"]["operations"][0]["daily_price"], 172.0)
        self.assertEqual(calls[0]["body"]["operations"][0]["min_length_of_stay"], 2)

    @patch.dict(
        "os.environ",
        {
            "SMOOBU_API_CONSUMER_KEY": "key",
            "SMOOBU_API_CONSUMER_SECRET": "secret",
            "SMOOBU_APARTMENT_ID": "123",
        },
        clear=False,
    )
    def test_channel_manager_recognizes_smoobu_as_supported(self):
        prop = {
            "id": 1,
            "platform": "smoobu",
            "listing_id": "123",
        }

        status = get_channel_manager().get_status(prop)

        self.assertTrue(status["supported"])
        self.assertTrue(status["token_set"])
        self.assertTrue(status["is_real"])


if __name__ == "__main__":
    unittest.main()
