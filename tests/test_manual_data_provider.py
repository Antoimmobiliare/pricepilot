from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from pricepilot.providers.manual import (
    ManualEventProvider,
    ManualMarketDataProvider,
    ManualOccupancyProvider,
)
from pricepilot.providers.registry import (
    get_market_data_provider,
    get_occupancy_provider,
    reset_providers,
)


class ManualDataProviderTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        reset_providers()
        self.tmp.cleanup()

    def test_manual_market_provider_reads_csv_and_builds_stats(self):
        csv_path = self.root / "market.csv"
        csv_path.write_text(
            "account_id,property_id,date,competitor_name,price\n"
            "1,7,2026-07-15,A,100\n"
            "1,7,2026-07-15,B,140\n"
            "1,8,2026-07-15,C,999\n",
            encoding="utf-8",
        )
        provider = ManualMarketDataProvider(str(csv_path))

        result = provider.analyze(
            account_id=1,
            property_id=7,
            target_date=date(2026, 7, 15),
            persist=False,
        )

        self.assertEqual(result.source, "manual_csv_market")
        self.assertEqual(result.market_stats["competitor_count"], 2)
        self.assertEqual(result.market_stats["market_avg"], 120.0)

    def test_manual_event_and_occupancy_providers_read_csv(self):
        events_path = self.root / "events.csv"
        occupancy_path = self.root / "occupancy.csv"
        events_path.write_text(
            "date,name,event_type,impact_level,description\n"
            "2026-10-31,Lucca Comics,festival,high,Alta domanda\n",
            encoding="utf-8",
        )
        occupancy_path.write_text(
            "account_id,property_id,date,occupancy\n"
            "1,7,2026-10-31,95\n",
            encoding="utf-8",
        )

        event_provider = ManualEventProvider(str(events_path))
        occupancy_provider = ManualOccupancyProvider(str(occupancy_path))

        event = event_provider.event_for_date(date(2026, 10, 31))
        occupancy = occupancy_provider.estimate(
            account_id=1,
            property_id=7,
            target_date=date(2026, 10, 31),
        )

        self.assertEqual(event_provider.event_to_string(event), "festival")
        self.assertEqual(occupancy.occupancy, 0.95)

    def test_registry_switches_to_manual_provider_from_env(self):
        env = {
            "PRICEPILOT_DATA_PROVIDER": "manual",
            "PRICEPILOT_MANUAL_MARKET_CSV": str(self.root / "missing_market.csv"),
            "PRICEPILOT_MANUAL_OCCUPANCY_CSV": str(self.root / "missing_occupancy.csv"),
        }

        with patch.dict("os.environ", env, clear=True):
            reset_providers()

            self.assertEqual(get_market_data_provider().name, "manual_csv_market")
            self.assertEqual(get_occupancy_provider().name, "manual_csv_occupancy")


if __name__ == "__main__":
    unittest.main()
