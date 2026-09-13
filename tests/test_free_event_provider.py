from __future__ import annotations

import json
import unittest
from datetime import date
from unittest.mock import patch

from pricepilot.providers.free_events import FreeEventProvider


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FreeEventProviderTestCase(unittest.TestCase):
    def test_italian_national_holiday_needs_no_external_api(self):
        provider = FreeEventProvider(ticketmaster_api_key="")

        event = provider.event_for_property(
            prop={"city": "Lucca"},
            target_date=date(2026, 12, 25),
        )

        self.assertEqual(event["name"], "Natale")
        self.assertEqual(provider.event_to_string(event), "holiday")
        self.assertIn("Natale", provider.event_label(event))

    def test_ticketmaster_events_are_location_aware_and_cached(self):
        payload = {
            "_embedded": {
                "events": [{
                    "id": "tm-1",
                    "name": "Lucca Summer Festival",
                    "distance": 8.2,
                    "classifications": [{"segment": {"name": "Music"}}],
                    "_embedded": {"venues": [{"name": "Piazza Napoleone"}]},
                }]
            }
        }
        provider = FreeEventProvider(ticketmaster_api_key="test-key", radius_km=35)
        prop = {"city": "Lucca", "latitude": 43.84, "longitude": 10.50}

        with patch("pricepilot.providers.free_events.urlopen", return_value=_Response(payload)) as request:
            first = provider.event_for_property(prop=prop, target_date=date(2026, 7, 10))
            second = provider.event_for_property(prop=prop, target_date=date(2026, 7, 10))

        self.assertEqual(request.call_count, 1)
        self.assertEqual(provider.event_to_string(first), "festival")
        self.assertEqual(first["name"], second["name"])
        self.assertIn("8 km", provider.event_label(first))


if __name__ == "__main__":
    unittest.main()
