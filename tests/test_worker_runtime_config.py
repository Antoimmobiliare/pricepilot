import os
from datetime import date, timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from pricepilot.core.data_quality import DataUnavailable
from pricepilot.providers.observations import ObservedInventoryProvider
from pricepilot.providers import registry


class WorkerRuntimeConfigTests(TestCase):
    def tearDown(self):
        registry.reset_providers()

    def test_approval_worker_selects_authoritative_inventory_provider(self):
        with patch.dict(os.environ, {
            "PRICEPILOT_RUNTIME": "worker",
            "PRICEPILOT_DATABASE_BACKEND": "supabase",
            "PRICEPILOT_OCCUPANCY_PROVIDER": "observed_inventory",
            "PRICEPILOT_CHANNEL_PROVIDER": "beds24",
        }, clear=False):
            registry.reset_providers()
            self.assertEqual(registry.get_occupancy_provider().name, "observed_inventory")
            self.assertEqual(registry.get_channel_manager_provider().__class__.__name__, "Beds24ChannelProvider")

    def test_observed_inventory_reads_persisted_rows_and_fails_closed_when_incomplete(self):
        start = date(2026, 10, 12)
        rows = [
            {
                "account_id": 4,
                "property_id": 11,
                "date": (start + timedelta(days=offset)).isoformat(),
                "state": "booked" if offset == 0 else "open",
                "observed_at": "2026-10-06T17:50:00+00:00",
                "source_reference": "beds24:357389:736801:1",
            }
            for offset in range(30)
        ]
        with patch("pricepilot.services.operational_store.get_inventory_rows", return_value=rows):
            result = ObservedInventoryProvider().estimate(
                property_id=11, target_date=start, account_id=4,
            )
        self.assertEqual(result.source, "observed_inventory")
        self.assertEqual(result.raw["target_state"], "booked")
        self.assertEqual(result.raw["available_nights"], 30)
        self.assertAlmostEqual(result.occupancy, 1 / 30)

        with patch("pricepilot.services.operational_store.get_inventory_rows", return_value=rows[:-1]):
            with self.assertRaisesRegex(DataUnavailable, "Servono 30 giorni"):
                ObservedInventoryProvider().estimate(
                    property_id=11, target_date=start, account_id=4,
                )

    def test_poller_workflow_declares_same_runtime_provider_contract(self):
        workflow = Path(".github/workflows/telegram-approval-poller.yml").read_text(encoding="utf-8")
        self.assertIn("PRICEPILOT_OCCUPANCY_PROVIDER: observed_inventory", workflow)
        self.assertIn("PRICEPILOT_CHANNEL_PROVIDER: beds24", workflow)
        self.assertIn("PRICEPILOT_DATABASE_BACKEND: supabase", workflow)


if __name__ == "__main__":
    import unittest
    unittest.main()
