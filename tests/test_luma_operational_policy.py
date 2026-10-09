"""Approved owner configuration; no channel writes or live callbacks."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from pricepilot.engine.calendar_pricing import calculate_calendar_price, load_policy, validate_policy


class LumaOperationalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = json.loads(Path('config/policies/luma_pisa.json').read_text(encoding='utf-8'))
        self.now = datetime(2026, 10, 9, 11, 45, tzinfo=timezone.utc)

    def calculate(self, **changes):
        args = dict(current_price=89, occupancy=.23333333333333334,
                    target_date=date(2026, 10, 12), policy=self.policy,
                    min_price=79, max_price=160, max_change_pct=.2, now=self.now,
                    inventory_context=dict(metrics_complete=True, pickup_7d_nights=1,
                                           gap_nights=3, gap_boundaries_confirmed=True, minimum_stay=1))
        args.update(changes)
        return calculate_calendar_price(**args)

    def test_approved_configuration_preserves_owner_limits(self):
        validate_policy(self.policy)
        self.assertEqual(self.policy['reference_price'], 89)
        self.assertEqual(self.policy['break_even'], 79)
        self.assertEqual(self.policy['weekend_multiplier'], 1.056)
        self.assertEqual(self.policy['minimum_change_eur'], 2)
        self.assertEqual(self.policy['unsold_risk']['minimum_negative_signals'], 2)
        self.assertEqual(self.policy['unsold_risk']['max_total_discount'], .08)

    def test_observed_near_date_has_moderate_non_stacking_discount(self):
        result = self.calculate()
        self.assertEqual(result['recommended_price'], 84.55)
        self.assertEqual(result['breakdown']['effective_multiplier'], .95)
        self.assertEqual(result['breakdown']['gap_multiplier'], .96)
        self.assertEqual(self.calculate(current_price=84.55)['recommended_price'], 84.55)

    def test_weak_pickup_alone_and_distant_low_occupancy_do_not_discount(self):
        self.assertEqual(self.calculate(occupancy=.5,
            inventory_context=dict(metrics_complete=True, pickup_7d_nights=0))['recommended_price'], 89)
        self.assertEqual(self.calculate(target_date=date(2026, 11, 9), occupancy=0,
            inventory_context=dict(metrics_complete=True, pickup_7d_nights=0))['recommended_price'], 89)

    def test_gap_requires_confirmed_boundaries_and_compatible_stay(self):
        for context in [dict(gap_nights=3, gap_boundaries_confirmed=False, minimum_stay=1),
                        dict(gap_nights=3, gap_boundaries_confirmed=True, minimum_stay=4),
                        dict(gap_nights=4, gap_boundaries_confirmed=True, minimum_stay=1)]:
            with self.subTest(context=context):
                self.assertEqual(self.calculate(occupancy=.5, inventory_context=context)['recommended_price'], 89)
        self.assertEqual(self.calculate(occupancy=.5,
            inventory_context=dict(gap_nights=3, gap_boundaries_confirmed=True, minimum_stay=1))['recommended_price'], 85.44)

    def test_urgency_requires_evidence_and_amplifies_progressively(self):
        prices = []
        for hours in (60, 36, 12, 8):
            target = datetime(2026, 10, 12, 13, tzinfo=timezone.utc)
            now = target-timedelta(hours=hours)
            result = self.calculate(now=now,
                inventory_context=dict(metrics_complete=True, pickup_7d_nights=0))
            prices.append(result['recommended_price'])
            self.assertGreaterEqual(result['recommended_price'], 79)
            self.assertGreaterEqual(result['breakdown']['effective_multiplier'], .92)
            neutral = self.calculate(now=now, occupancy=.5,
                inventory_context=dict(metrics_complete=True, pickup_7d_nights=3))
            self.assertEqual(neutral['recommended_price'], 89)
        self.assertEqual(prices, sorted(prices, reverse=True))
        self.assertGreater(prices[0], prices[-1])

    def test_weekend_premium_and_positive_occupancy_are_preserved(self):
        result = self.calculate(target_date=date(2026, 10, 16), current_price=94, occupancy=.2,
            inventory_context=dict(metrics_complete=True, pickup_7d_nights=0))
        self.assertEqual(result['recommended_price'], 89.28)
        self.assertEqual(result['breakdown']['weekend_multiplier'], 1.056)
        self.assertEqual(self.calculate(occupancy=.9, inventory_context={})['recommended_price'], 93.45)

    def test_worker_loads_canonical_saved_policy_not_file_fallback(self):
        with patch.dict(os.environ, {'PRICEPILOT_RUNTIME': 'worker',
                                    'PRICEPILOT_DATABASE_BACKEND': 'supabase'}), \
             patch('pricepilot.services.operational_store.get_calendar_policy', return_value=deepcopy(self.policy)) as read:
            self.assertEqual(load_policy(4, 11), self.policy)
            read.assert_called_once_with(4, 11)

