"""Own-calendar workflow: no market access, mandatory human approval."""
from datetime import date, datetime, timedelta
from copy import deepcopy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from pricepilot.core import database as db
from pricepilot.core.config import CONFIG
from pricepilot.core.data_quality import DataUnavailable
from pricepilot.engine.calendar_pricing import calculate_calendar_price, enrich_gap_context, load_policy
from pricepilot.engine import decision_engine as engine
from pricepilot.providers.contracts import OccupancyResult
from pricepilot.api import server


def policy():
    return {'reference_price': 100, 'weekend_multiplier': 1, 'break_even': 0,
            'lead_time_bands': [
                {'through_days': 7, 'low_occupancy': .4, 'high_occupancy': .8,
                 'low_multiplier': .9, 'high_multiplier': 1.05},
                {'through_days': 366, 'low_occupancy': .1, 'high_occupancy': .7,
                 'low_multiplier': 1, 'high_multiplier': 1.05}]}


def risk_policy():
    own = policy()
    own['pacing_rule'] = {'enabled': True, 'through_days': 60,
        'low_pickup_7d_nights': 1, 'high_pickup_7d_nights': 5,
        'low_multiplier': .95, 'high_multiplier': 1.08}
    own['gap_rule'] = {'enabled': True, 'max_nights': 3, 'through_days': 21,
                       'multiplier': .90}
    own['unsold_risk'] = {
        'enabled': True,
        'urgency_weights': {'STANDARD': 0, 'WATCH': .15, 'LAST_MINUTE': .4,
                            'URGENT': .7, 'SAME_DAY': 1},
        'max_amplification': .5,
        'max_total_discount': .15,
        'minimum_negative_signals': 2,
    }
    return own


class CalendarRuleTests(unittest.TestCase):
    def calculate(self, **changes):
        args = dict(current_price=100, occupancy=.2,
            target_date=date.today()+timedelta(days=3), policy=policy(),
            min_price=50, max_price=200, max_change_pct=.2)
        args.update(changes)
        return calculate_calendar_price(**args)

    def test_calendar_rules_need_no_market_or_fabricated_adr(self):
        result = self.calculate()
        self.assertEqual(result['recommended_price'], 90)
        self.assertIsNone(result['delta_vs_market'])
        self.assertIsNone(result['breakdown']['adr'])
        self.assertEqual(result['confidence_kind'], 'not_estimated_rules_only')

    def test_far_out_empty_calendar_does_not_force_discount(self):
        args = dict(current_price=100, occupancy=0, target_date=date.today()+timedelta(days=90),
                    policy=policy(), min_price=50, max_price=200, max_change_pct=.2)
        self.assertEqual(calculate_calendar_price(**args)['recommended_price'], 100)

    def test_repeated_cycle_after_approval_does_not_compound_discount(self):
        args = dict(current_price=90, occupancy=.2, target_date=date.today()+timedelta(days=3),
                    policy=policy(), min_price=50, max_price=200, max_change_pct=.2)
        self.assertEqual(calculate_calendar_price(**args)['recommended_price'], 90)

    def test_pacing_never_stacks_discounts_and_high_pickup_blocks_reduction(self):
        own = policy()
        own['pacing_rule'] = {'enabled': True, 'through_days': 60,
            'low_pickup_7d_nights': 1, 'high_pickup_7d_nights': 5,
            'low_multiplier': .95, 'high_multiplier': 1.08}
        low = self.calculate(policy=own, inventory_context={
            'metrics_complete': True, 'pickup_7d_nights': 0})
        high = self.calculate(policy=own, inventory_context={
            'metrics_complete': True, 'pickup_7d_nights': 7})
        self.assertEqual(low['recommended_price'], 90)
        self.assertEqual(high['recommended_price'], 100)
        self.assertTrue(high['breakdown']['signal_conflict'])
        self.assertEqual(high['breakdown']['manual_actions'][0]['type'], 'pricing_signal_conflict')
        self.assertEqual(low['breakdown']['unbounded_reference_target'], 90)

    def test_conflicting_low_occupancy_and_high_pickup_keeps_weekend_price(self):
        own = policy()
        own['weekend_multiplier'] = 1.15
        own['pacing_rule'] = {'enabled': True, 'through_days': 60,
            'low_pickup_7d_nights': 1, 'high_pickup_7d_nights': 5,
            'low_multiplier': .95, 'high_multiplier': 1.08}
        target = date.today() + timedelta(days=(4-date.today().weekday()) % 7)
        result = calculate_calendar_price(
            current_price=117, occupancy=.2, target_date=target, policy=own,
            min_price=50, max_price=200, max_change_pct=.2,
            inventory_context={'metrics_complete': True, 'pickup_7d_nights': 7},
        )
        self.assertEqual(result['recommended_price'], 117)
        self.assertTrue(result['breakdown']['signal_conflict'])
        self.assertIn('prezzo invariato', result['reason'])

    def test_low_pickup_alone_does_not_trigger_discount(self):
        own = policy()
        own['pacing_rule'] = {'enabled': True, 'through_days': 60,
            'low_pickup_7d_nights': 1, 'high_pickup_7d_nights': 5,
            'low_multiplier': .95, 'high_multiplier': 1.08}
        result = self.calculate(policy=own, occupancy=.6, inventory_context={
            'metrics_complete': True, 'pickup_7d_nights': 0})
        self.assertEqual(result['recommended_price'], 100)

    def test_minimum_stay_is_manual_advice_not_an_automatic_change(self):
        own = policy()
        own['gap_rule'] = {'enabled': True, 'max_nights': 3, 'through_days': 14,
                           'multiplier': .90}
        own['minimum_stay_rule'] = {'enabled': True, 'through_days': 14,
                                    'max_gap_nights': 3}
        result = self.calculate(policy=own, occupancy=.6, inventory_context={
            'gap_nights': 2, 'gap_boundaries_confirmed': True, 'minimum_stay': 3})
        self.assertEqual(result['recommended_price'], 100)
        action = result['breakdown']['manual_actions'][0]
        self.assertEqual(action['suggested_minimum_stay'], 2)

    def test_only_fresh_complete_metrics_enter_pacing_context(self):
        now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc)
        with patch('pricepilot.services.operational_store.get_reservation_metrics', return_value={
                'complete': True, 'observed_at': now.isoformat(), 'pickup_7d_nights': 4}), \
             patch('pricepilot.services.operational_store.get_inventory_rows', return_value=[]):
            context = enrich_gap_context(1, 2, date.today(), {}, policy())
        self.assertEqual(context['pickup_7d_nights'], 4)
        self.assertTrue(context['metrics_complete'])

    def test_rejected_proposal_is_not_reused(self):
        prior = {'policy_fingerprint': 'x', 'current_price_source': 'beds24_observation',
                 'reference_price': 100, 'occupancy_multiplier': .9,
                 'pacing_multiplier': 1, 'pickup_7d_nights': 0,
                 'weekend_multiplier': 1, 'lead_time_days': 3,
                 'gap_multiplier': 1, 'gap_nights': None,
                 'effective_multiplier': .9, 'manual_actions': []}
        row = {'old_price': 100, 'new_price': 90, 'mode': 'approval',
               'decision': 'PENDING_APPROVAL [REJECTED]', 'factors': json.dumps(prior)}
        self.assertFalse(engine._reuse_proposal(row, 100, 90, prior, 'approval'))

    def test_only_valid_dedup_states_are_reused(self):
        prior = {'policy_fingerprint': 'x', 'current_price_source': 'beds24_observation',
                 'reference_price': 100, 'occupancy_multiplier': .9,
                 'pacing_multiplier': 1, 'pickup_7d_nights': 0,
                 'weekend_multiplier': 1, 'lead_time_days': 3,
                 'lead_time_band': 'WATCH', 'checkin_datetime': '2026-10-10T15:00:00+02:00',
                 'timezone': 'Europe/Rome', 'gap_multiplier': 1, 'gap_nights': None,
                 'effective_multiplier': .9, 'manual_actions': []}

        def row(state, **extra):
            return {'old_price': 100, 'new_price': 90, 'mode': 'approval',
                    'decision': state, 'factors': json.dumps(prior),
                    'timestamp': datetime.now(ZoneInfo('UTC')).isoformat(), **extra}

        self.assertTrue(engine._reuse_proposal(row('PENDING_APPROVAL'), 100, 90, prior, 'approval'))
        self.assertTrue(engine._reuse_proposal(row('UNCHANGED: tariffa gia allineata'), 100, 90, prior, 'approval'))
        self.assertFalse(engine._reuse_proposal(row('PENDING_APPROVAL [APPROVED_SYNC_FAILED]'), 100, 90, prior, 'approval'))
        self.assertFalse(engine._reuse_proposal(row('PENDING_APPROVAL [REJECTED]'), 100, 90, prior, 'approval'))
        self.assertFalse(engine._reuse_proposal(row('PENDING_APPROVAL', applied=1), 100, 90, prior, 'approval'))
        stale = datetime.now(ZoneInfo('UTC')) - timedelta(hours=7)
        self.assertFalse(engine._reuse_proposal(row('PENDING_APPROVAL', timestamp=stale.isoformat()), 100, 90, prior, 'approval'))

    def test_date_reference_and_explicit_weekend_rule(self):
        own = policy()
        day = date.today()+timedelta(days=3)
        own['date_reference_prices'] = {day.isoformat(): 120}
        result = calculate_calendar_price(current_price=120, occupancy=.2, target_date=day,
                                         policy=own, min_price=50, max_price=200, max_change_pct=.2)
        self.assertEqual(result['recommended_price'], 108)

    def test_missing_or_cross_account_policy_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'policy.json'
            own = {**policy(), 'enabled': True, 'account_id': 1, 'property_id': 2}
            path.write_text(json.dumps({'schema_version': 'pricepilot.calendar-policy.v1', 'properties': [own]}))
            with patch.dict(os.environ, {'PRICEPILOT_CALENDAR_POLICY_FILE': str(path)}), \
                 patch('pricepilot.services.operational_store.get_calendar_policy', return_value=None):
                self.assertEqual(load_policy(1, 2)['reference_price'], 100)
                with self.assertRaises(DataUnavailable): load_policy(99, 2)

    def test_hourly_lead_time_band_boundaries(self):
        zone = ZoneInfo('Europe/Rome')
        checkin = datetime(2026, 10, 10, 15, 0, tzinfo=zone)
        cases = [
            (73, 'STANDARD'),
            (72, 'WATCH'),
            (60, 'WATCH'),
            (48, 'LAST_MINUTE'),
            (36, 'LAST_MINUTE'),
            (24, 'URGENT'),
            (17, 'URGENT'),
        ]
        for hours, expected in cases:
            with self.subTest(hours=hours):
                result = self.calculate(
                    target_date=checkin.date(), now=checkin-timedelta(hours=hours))
                self.assertEqual(result['lead_time_band'], expected)
                self.assertAlmostEqual(result['hours_until_checkin'], hours)
                self.assertEqual(result['breakdown']['lead_time_band'], expected)
                self.assertEqual(result['breakdown']['timezone'], 'Europe/Rome')

    def test_naive_current_datetime_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'fuso orario'):
            self.calculate(target_date=date(2026, 10, 10),
                           now=datetime(2026, 10, 9, 15, 0))

    def test_same_day_before_checkin_and_passed_checkin(self):
        zone = ZoneInfo('Europe/Rome')
        target = date(2026, 10, 10)
        before = self.calculate(target_date=target,
                                now=datetime(2026, 10, 10, 10, 30, tzinfo=zone))
        self.assertEqual(before['lead_time_band'], 'SAME_DAY')
        self.assertAlmostEqual(before['hours_until_checkin'], 4.5)
        with self.assertRaisesRegex(DataUnavailable, 'Check-in già trascorso'):
            self.calculate(target_date=target,
                           now=datetime(2026, 10, 10, 15, 0, tzinfo=zone))
        with self.assertRaisesRegex(DataUnavailable, 'Check-in già trascorso'):
            self.calculate(target_date=target,
                           now=datetime(2026, 10, 10, 15, 1, tzinfo=zone))

    def test_timezone_and_dst_use_actual_elapsed_hours(self):
        zone = ZoneInfo('Europe/Rome')
        result = self.calculate(
            target_date=date(2026, 3, 30),
            now=datetime(2026, 3, 28, 15, 0, tzinfo=zone),
        )
        self.assertEqual(result['timezone'], 'Europe/Rome')
        self.assertEqual(result['checkin_datetime'], '2026-03-30T15:00:00+02:00')
        self.assertAlmostEqual(result['hours_until_checkin'], 47)
        self.assertEqual(result['lead_time_band'], 'LAST_MINUTE')

    def test_gap_and_last_minute_are_both_preserved_without_stacking(self):
        own = policy()
        own['gap_rule'] = {'enabled': True, 'max_nights': 2, 'through_days': 21,
                           'multiplier': .9}
        zone = ZoneInfo('Europe/Rome')
        checkin = datetime(2026, 10, 10, 15, 0, tzinfo=zone)
        result = self.calculate(
            target_date=checkin.date(), now=checkin-timedelta(hours=30), policy=own,
            occupancy=.6, inventory_context={'gap_nights': 1,
                'gap_boundaries_confirmed': True, 'minimum_stay': 1},
        )
        self.assertEqual(result['lead_time_band'], 'LAST_MINUTE')
        self.assertEqual(result['breakdown']['gap_nights'], 1)
        self.assertEqual(result['breakdown']['gap_multiplier'], .9)
        self.assertEqual(result['breakdown']['effective_multiplier'], .9)

    def test_existing_policy_without_time_fields_uses_safe_defaults(self):
        zone = ZoneInfo('Europe/Rome')
        result = self.calculate(
            target_date=date(2026, 10, 10),
            now=datetime(2026, 10, 9, 15, 0, tzinfo=zone),
        )
        self.assertEqual(result['timezone'], 'Europe/Rome')
        self.assertEqual(result['checkin_datetime'], '2026-10-10T15:00:00+02:00')
        self.assertEqual(result['lead_time_band'], 'URGENT')

    def test_checkin_time_and_timezone_are_overridable_per_policy(self):
        own = policy()
        own['checkin_time'] = '17:30'
        own['timezone'] = 'America/New_York'
        zone = ZoneInfo('America/New_York')
        result = self.calculate(
            target_date=date(2026, 10, 10), policy=own,
            now=datetime(2026, 10, 10, 16, 0, tzinfo=zone),
        )
        self.assertEqual(result['timezone'], 'America/New_York')
        self.assertEqual(result['checkin_datetime'], '2026-10-10T17:30:00-04:00')
        self.assertAlmostEqual(result['hours_until_checkin'], 1.5)
        self.assertEqual(result['lead_time_band'], 'SAME_DAY')

    def test_urgency_band_does_not_change_price_automatically(self):
        zone = ZoneInfo('Europe/Rome')
        checkin = datetime(2026, 10, 10, 15, 0, tzinfo=zone)
        last_minute = self.calculate(
            target_date=checkin.date(), now=checkin-timedelta(hours=25))
        urgent = self.calculate(
            target_date=checkin.date(), now=checkin-timedelta(hours=23))
        self.assertEqual(last_minute['lead_time_band'], 'LAST_MINUTE')
        self.assertEqual(urgent['lead_time_band'], 'URGENT')
        self.assertEqual(last_minute['days_until'], urgent['days_until'])
        self.assertEqual(last_minute['recommended_price'], urgent['recommended_price'])
        self.assertEqual(last_minute['breakdown']['effective_multiplier'],
                         urgent['breakdown']['effective_multiplier'])

    def risk_calculate(self, hours, *, occupancy=.2, context=None, current_price=100,
                       own=None, min_price=50, break_even=None, max_change_pct=.2):
        zone = ZoneInfo('Europe/Rome')
        checkin = datetime(2026, 10, 10, 15, 0, tzinfo=zone)
        configured = deepcopy(own or risk_policy())
        if break_even is not None:
            configured['break_even'] = break_even
        return self.calculate(
            current_price=current_price, occupancy=occupancy,
            target_date=checkin.date(), now=checkin-timedelta(hours=hours),
            policy=configured, inventory_context=context or {}, min_price=min_price,
            max_change_pct=max_change_pct,
        )

    def test_standard_open_date_and_watch_healthy_do_not_discount_for_urgency(self):
        standard = self.risk_calculate(73, occupancy=.6)
        watch = self.risk_calculate(60, occupancy=.6, context={
            'metrics_complete': True, 'pickup_7d_nights': 3})
        self.assertEqual(standard['recommended_price'], 100)
        self.assertEqual(watch['recommended_price'], 100)
        self.assertEqual(standard['breakdown']['unsold_risk_pressure'], 0)
        self.assertEqual(watch['breakdown']['urgency_action'], 'hold_no_negative_signals')

    def test_negative_signals_are_progressively_amplified_by_urgency(self):
        context = {'metrics_complete': True, 'pickup_7d_nights': 0}
        results = [self.risk_calculate(hours, context=context)
                   for hours in (60, 36, 17, 4)]
        prices = [result['recommended_price'] for result in results]
        pressures = [result['breakdown']['unsold_risk_pressure'] for result in results]
        self.assertGreater(prices[0], prices[1])
        self.assertGreater(prices[1], prices[2])
        self.assertGreater(prices[2], prices[3])
        self.assertEqual(pressures, sorted(pressures))
        self.assertTrue(all(result['breakdown']['urgency_action'] == 'amplify_negative_signals'
                            for result in results))

    def test_single_weak_occupancy_keeps_base_rule_without_urgency_amplification(self):
        cases = [(60, 'WATCH'), (36, 'LAST_MINUTE'),
                 (17, 'URGENT'), (4, 'SAME_DAY')]
        for hours, expected_band in cases:
            with self.subTest(band=expected_band):
                result = self.risk_calculate(hours, occupancy=.2)
                self.assertEqual(result['lead_time_band'], expected_band)
                self.assertEqual(result['recommended_price'], 90)
                self.assertEqual(result['breakdown']['base_effective_multiplier'], .9)
                self.assertEqual(result['breakdown']['effective_multiplier'], .9)
                self.assertEqual(result['breakdown']['urgency_action'],
                                 'hold_insufficient_negative_evidence')

    def test_single_weak_pickup_keeps_existing_base_economics(self):
        result = self.risk_calculate(17, occupancy=.6, context={
            'metrics_complete': True, 'pickup_7d_nights': 0})
        self.assertEqual(result['lead_time_band'], 'URGENT')
        self.assertEqual(result['recommended_price'], 100)
        self.assertEqual(result['breakdown']['pacing_multiplier'], .95)
        self.assertEqual(result['breakdown']['base_effective_multiplier'], 1)
        self.assertEqual(result['breakdown']['effective_multiplier'], 1)
        self.assertEqual(result['breakdown']['urgency_action'],
                         'hold_insufficient_negative_evidence')

    def test_urgent_and_same_day_positive_signals_never_become_discounts(self):
        cases = [
            ('occupancy_strong', .9, {}, 105),
            ('pickup_strong', .6,
             {'metrics_complete': True, 'pickup_7d_nights': 7}, 108),
            ('both_strong', .9,
             {'metrics_complete': True, 'pickup_7d_nights': 7}, 108),
        ]
        for hours, band in ((17, 'URGENT'), (4, 'SAME_DAY')):
            for label, occupancy, context, expected_price in cases:
                with self.subTest(band=band, signals=label):
                    result = self.risk_calculate(hours, occupancy=occupancy,
                                                 context=context)
                    self.assertEqual(result['lead_time_band'], band)
                    self.assertEqual(result['recommended_price'], expected_price)
                    self.assertGreaterEqual(result['breakdown']['effective_multiplier'], 1)
                    self.assertEqual(result['breakdown']['urgency_action'],
                                     'hold_positive_signals')

    def test_risk_conflict_blocks_only_amplification_not_valid_base_increase(self):
        result = self.risk_calculate(17, occupancy=.9, context={
            'metrics_complete': True, 'pickup_7d_nights': 0})
        self.assertTrue(result['breakdown']['signal_conflict'])
        self.assertFalse(result['breakdown']['base_signal_conflict'])
        self.assertEqual(result['breakdown']['base_effective_multiplier'], 1.05)
        self.assertEqual(result['breakdown']['effective_multiplier'], 1.05)
        self.assertEqual(result['recommended_price'], 105)
        self.assertEqual(result['breakdown']['urgency_action'], 'hold_signal_conflict')

        legacy_conflict = self.risk_calculate(17, occupancy=.2, context={
            'metrics_complete': True, 'pickup_7d_nights': 7})
        self.assertTrue(legacy_conflict['breakdown']['base_signal_conflict'])
        self.assertEqual(legacy_conflict['recommended_price'], 100)

    def test_last_minute_and_same_day_positive_signals_do_not_force_floor(self):
        context = {'metrics_complete': True, 'pickup_7d_nights': 7}
        last_minute = self.risk_calculate(36, occupancy=.9, context=context,
                                          current_price=108)
        same_day = self.risk_calculate(4, occupancy=.9, context=context,
                                      current_price=108)
        self.assertGreaterEqual(last_minute['recommended_price'], 100)
        self.assertGreaterEqual(same_day['recommended_price'], 100)
        self.assertEqual(same_day['breakdown']['urgency_action'], 'hold_positive_signals')

    def test_urgent_positive_pickup_is_less_aggressive_and_reports_conflict(self):
        weak = self.risk_calculate(17, context={
            'metrics_complete': True, 'pickup_7d_nights': 0})
        positive = self.risk_calculate(17, context={
            'metrics_complete': True, 'pickup_7d_nights': 7})
        self.assertGreater(positive['recommended_price'], weak['recommended_price'])
        self.assertTrue(positive['breakdown']['signal_conflict'])
        self.assertEqual(positive['breakdown']['rules_confidence'], 'conflicted')
        self.assertEqual(positive['breakdown']['urgency_action'], 'hold_signal_conflict')
        self.assertTrue(positive['breakdown']['negative_signals'])
        self.assertTrue(positive['breakdown']['positive_signals'])

    def test_confirmed_gap_strengthens_urgent_pressure_without_stacking(self):
        pacing = {'metrics_complete': True, 'pickup_7d_nights': 0}
        normal = self.risk_calculate(17, context=pacing)
        gap = self.risk_calculate(17, context={**pacing, 'gap_nights': 1,
            'gap_boundaries_confirmed': True, 'minimum_stay': 1})
        self.assertLess(gap['recommended_price'], normal['recommended_price'])
        self.assertGreaterEqual(gap['breakdown']['effective_multiplier'], .85)
        self.assertGreater(gap['breakdown']['effective_multiplier'], .9*.95*.9)
        self.assertEqual(gap['breakdown']['urgency_action'], 'amplify_negative_signals')

    def test_consecutive_open_nights_are_not_invented_as_isolated_gap(self):
        result = self.risk_calculate(12, occupancy=.6, context={
            'metrics_complete': True, 'pickup_7d_nights': 0,
            'consecutive_open_nights': 4})
        self.assertEqual(result['breakdown']['gap_multiplier'], 1)
        self.assertNotIn('confirmed_isolated_gap',
                         [item['signal'] for item in result['breakdown']['negative_signals']])
        self.assertEqual(result['recommended_price'], 100)

    def test_reference_price_does_not_compound_unsold_risk(self):
        context = {'metrics_complete': True, 'pickup_7d_nights': 0}
        first = self.risk_calculate(12, context=context)
        second = self.risk_calculate(12, context=context,
                                     current_price=first['recommended_price'])
        self.assertEqual(second['recommended_price'], first['recommended_price'])
        self.assertEqual(second['breakdown']['reference_price'], 100)

    def test_unsold_risk_respects_floor_break_even_and_max_change(self):
        context = {'metrics_complete': True, 'pickup_7d_nights': 0,
                   'gap_nights': 1, 'gap_boundaries_confirmed': True,
                   'minimum_stay': 1}
        floor = self.risk_calculate(4, context=context, min_price=92, break_even=94)
        limited = self.risk_calculate(4, context=context, max_change_pct=.05)
        self.assertGreaterEqual(floor['recommended_price'], 94)
        self.assertGreaterEqual(limited['recommended_price'], 95)

    def test_max_total_discount_caps_risk_component_before_weekend_and_safety(self):
        context = {'metrics_complete': True, 'pickup_7d_nights': 0,
                   'gap_nights': 1, 'gap_boundaries_confirmed': True,
                   'minimum_stay': 1}
        own = risk_policy()
        own['weekend_multiplier'] = 1.1
        result = self.risk_calculate(4, context=context, own=own)
        self.assertEqual(result['breakdown']['base_effective_multiplier'], .9)
        self.assertAlmostEqual(result['breakdown']['effective_multiplier'], .85)
        self.assertEqual(result['breakdown']['unbounded_reference_target'], 93.5)
        self.assertEqual(result['recommended_price'], 93.5)

        protected = self.risk_calculate(4, context=context, own=own,
                                        min_price=92, break_even=94,
                                        max_change_pct=.05)
        self.assertEqual(protected['recommended_price'], 95)

    def test_legacy_policy_preserves_previous_economics_and_reports_disabled(self):
        legacy = policy()
        zone = ZoneInfo('Europe/Rome')
        checkin = datetime(2026, 10, 10, 15, 0, tzinfo=zone)
        result = self.calculate(target_date=checkin.date(),
            now=checkin-timedelta(hours=12), policy=legacy,
            inventory_context={'metrics_complete': True, 'pickup_7d_nights': 0})
        self.assertEqual(result['recommended_price'], 90)
        self.assertEqual(result['breakdown']['urgency_action'], 'disabled_legacy')

    def test_unsold_risk_configuration_is_bounded_and_ordered(self):
        invalid = risk_policy()
        invalid['unsold_risk']['urgency_weights']['WATCH'] = .9
        invalid['unsold_risk']['urgency_weights']['LAST_MINUTE'] = .2
        with self.assertRaisesRegex(ValueError, 'crescere progressivamente'):
            self.calculate(policy=invalid)

    def test_urgency_band_change_invalidates_proposal_deduplication(self):
        prior = {'policy_fingerprint': 'x', 'current_price_source': 'beds24_observation',
                 'reference_price': 100, 'occupancy_multiplier': 1,
                 'pacing_multiplier': 1, 'pickup_7d_nights': 0,
                 'weekend_multiplier': 1, 'lead_time_days': 2,
                 'lead_time_band': 'WATCH', 'checkin_datetime': '2026-10-10T15:00:00+02:00',
                 'timezone': 'Europe/Rome', 'gap_multiplier': 1,
                 'gap_nights': None, 'effective_multiplier': 1, 'manual_actions': []}
        row = {'old_price': 100, 'new_price': 100, 'mode': 'approval',
               'decision': 'PENDING_APPROVAL', 'factors': json.dumps(prior),
               'timestamp': datetime.now(ZoneInfo('UTC')).isoformat()}
        changed = {**prior, 'lead_time_band': 'URGENT', 'lead_time_days': 1}
        self.assertFalse(engine._reuse_proposal(row, 100, 100, changed, 'approval'))


class CalendarWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory())
        old_db = CONFIG['db_path']
        CONFIG['db_path'] = str(Path(temporary)/'calendar.db')
        self.stack.callback(lambda: CONFIG.update(db_path=old_db))
        self.stack.enter_context(patch.dict(os.environ, {'PRICEPILOT_ENV': 'staging',
            'PRICEPILOT_DATA_PROVIDER': 'unconfigured', 'PRICEPILOT_DATABASE_BACKEND': 'sqlite',
            'PRICEPILOT_ALLOW_CHANNEL_WRITES': '1'}))
        db.init_db()
        account = db.create_account('Calendar fixture', plan='pro', billing_status='active')
        self.account_id = account['id']
        from pricepilot.services.property_service import create_property
        self.prop = create_property({'account_id': self.account_id, 'name': 'Calendar fixture',
            'min_price': 50, 'max_price': 200, 'plan': 'pro', 'sync_mode': 'auto', 'platform': 'airbnb'})
        self.day = date.today()+timedelta(days=3)
        self.calendar = {'account_id': self.account_id, 'property_id': self.prop['id'],
                         'date': self.day.isoformat(), 'current_price': 100,
                         'current_price_source': 'beds24_observation'}
        db.upsert_calendar_price(self.calendar)
        self.policy = policy()
        self.stack.enter_context(patch.object(engine, 'load_policy', side_effect=lambda *a: deepcopy(self.policy)))
        self.market = self.stack.enter_context(patch.object(engine, 'get_market_data_provider',
            side_effect=AssertionError('Calendar workflow must not request competitor data')))
        self.inventory = Mock()
        self.inventory.estimate.return_value = OccupancyResult(.2, 'observed_inventory', {'target_state': 'open'})
        self.stack.enter_context(patch('pricepilot.providers.registry.get_occupancy_provider', return_value=self.inventory))
        self.telegram = self.stack.enter_context(patch.object(engine, '_telegram_send_approval', return_value=True))
        self.channel = self.stack.enter_context(patch.object(engine, '_channel_manager_update',
            return_value={'ok': True, 'is_real': True, 'platform': 'test', 'listing_id': 'test'}))

    def run_decision(self):
        return engine.process_decision(property_id=self.prop['id'], target_date=self.day,
                                       force_mode='auto', account_id=self.account_id)

    def test_proposal_and_approval_work_without_any_competitor_data(self):
        result = self.run_decision()
        self.assertEqual(result['mode'], 'approval')
        self.assertFalse(result['applied'])
        self.assertEqual(result['recommended_price'], 90)
        self.assertIsNone(result['market_stats']['market_avg'])
        self.assertEqual(result['competitors'], [])
        self.channel.assert_not_called()
        self.telegram.assert_called_once()
        row = db.get_decision_log_entry(result['log_id'], self.account_id)
        self.assertEqual(row['data_source'], 'calendar_only')
        self.assertIsNone(row['competitor_avg'])
        self.assertTrue(engine.approve_decision(result['log_id'], self.account_id)['applied'])
        self.channel.assert_called_once()
        self.market.assert_not_called()

    def test_policy_changed_after_message_blocks_approval(self):
        result = self.run_decision()
        self.policy['reference_price'] = 110
        self.assertFalse(engine.approve_decision(result['log_id'], self.account_id)['applied'])
        self.channel.assert_not_called()

    def test_new_bookings_change_recommendation_before_approval(self):
        result = self.run_decision()
        self.inventory.estimate.return_value = OccupancyResult(.9, 'observed_inventory', {'target_state': 'open'})
        self.assertFalse(engine.approve_decision(result['log_id'], self.account_id)['applied'])
        self.channel.assert_not_called()

    def test_unchanged_price_sends_no_approval_message(self):
        db.upsert_calendar_price({**self.calendar, 'current_price': 90})
        result = self.run_decision()
        self.assertEqual(result['calendar_status'], 'unchanged')
        self.telegram.assert_not_called()
        self.channel.assert_not_called()

    def test_identical_pending_is_deduplicated_but_rejected_is_recalculated(self):
        first = self.run_decision()
        second = self.run_decision()
        self.assertTrue(second['deduplicated'])
        self.assertEqual(second['log_id'], first['log_id'])
        self.assertEqual(self.telegram.call_count, 1)
        db.mark_decision_rejected(first['log_id'], self.account_id)
        third = self.run_decision()
        self.assertFalse(third.get('deduplicated', False))
        self.assertNotEqual(third['log_id'], first['log_id'])
        self.assertEqual(self.telegram.call_count, 2)

    def test_changed_price_source_blocks_approval_even_when_amount_matches(self):
        result = self.run_decision()
        db.upsert_calendar_price({**self.calendar, 'current_price_source': 'manual',
                                  'decision_log_id': result['log_id'],
                                  'recommended_price': result['recommended_price'],
                                  'status': 'pending_approval'})
        blocked = engine.approve_decision(result['log_id'], self.account_id)
        self.assertEqual(blocked['status'], 'expired_or_missing_data')
        self.channel.assert_not_called()

    def test_changed_urgency_band_blocks_approval_revalidation(self):
        result = self.run_decision()
        row = db.get_decision_log_entry(result['log_id'], self.account_id)
        factors = json.loads(row['factors'])
        factors['lead_time_band'] = 'SAME_DAY'
        with db.get_conn() as conn:
            conn.execute('UPDATE decision_log SET factors=? WHERE id=?',
                         (json.dumps(factors), result['log_id']))
        blocked = engine.approve_decision(result['log_id'], self.account_id)
        self.assertEqual(blocked['status'], 'expired_or_missing_data')
        self.channel.assert_not_called()

    def test_attempted_channel_failure_is_not_reported_as_manual_sync(self):
        result = self.run_decision()
        self.channel.return_value = {'ok': False, 'is_real': False, 'attempted': True,
                                     'platform': 'beds24', 'error': 'timeout'}
        outcome = engine.approve_decision(result['log_id'], self.account_id)
        self.assertTrue(outcome['approved'])
        self.assertFalse(outcome['applied'])
        self.assertEqual(outcome['status'], 'approved_sync_failed')

    def test_write_gate_zero_keeps_approval_pending_without_claim_or_write(self):
        result = self.run_decision()
        with patch.dict(os.environ, {'PRICEPILOT_ALLOW_CHANNEL_WRITES': '0'}):
            outcome = engine.approve_decision(result['log_id'], self.account_id)
        self.assertEqual(outcome['status'], 'write_gate_blocked')
        self.assertFalse(outcome['applied'])
        self.channel.assert_not_called()
        row = db.get_decision_log_entry(result['log_id'], self.account_id)
        self.assertTrue(row['decision'].startswith('PENDING_APPROVAL'))
        self.assertFalse('[APPLYING]' in row['decision'])

    def test_approval_claim_is_scoped_to_one_pending_decision(self):
        first = self.run_decision()
        second_day = self.day + timedelta(days=1)
        db.upsert_calendar_price({**self.calendar, 'date': second_day.isoformat()})
        second = engine.process_decision(property_id=self.prop['id'], target_date=second_day,
                                         force_mode='auto', account_id=self.account_id)
        self.assertNotEqual(first['log_id'], second['log_id'])
        self.assertTrue(engine.approve_decision(first['log_id'], self.account_id)['applied'])
        self.assertEqual(self.channel.call_count, 1)
        untouched = db.get_decision_log_entry(second['log_id'], self.account_id)
        self.assertTrue(untouched['decision'].startswith('PENDING_APPROVAL'))
        self.assertFalse(untouched['applied'])

    def test_unknown_inventory_blocks_without_querying_market(self):
        self.inventory.estimate.side_effect = DataUnavailable('Synthetic missing calendar')
        with self.assertRaises(DataUnavailable): self.run_decision()
        self.market.assert_not_called()
        self.channel.assert_not_called()

    def test_telegram_message_has_no_fictitious_market_price(self):
        from pricepilot.services import telegram_bot
        with patch.object(telegram_bot, '_api_call', return_value={'ok': True}) as send:
            telegram_bot.send_approval_request(1, 'Fixture', 100, 90, .2, None, '', 123, 'Test rule')
        body = send.call_args.args[1]
        self.assertNotIn('Media mercato', body['text'])
        self.assertNotIn('competitor', body['text'].lower())
        self.assertEqual(len(body['reply_markup']['inline_keyboard'][0]), 2)

    def test_readiness_does_not_require_external_market_source(self):
        with patch.dict(os.environ, {'PRICEPILOT_ENV': 'production'}), \
             patch.object(server, 'get_market_data_provider', side_effect=AssertionError('No market access')), \
             patch.object(server, 'get_event_provider', side_effect=AssertionError('No event access')), \
             patch.object(server, 'get_occupancy_provider', return_value=SimpleNamespace(name='observed_inventory')):
            checks = server._deployment_readiness_checks()
        self.assertTrue(checks['data_providers']['ok'])


class TelegramCalendarWorkflowTests(unittest.TestCase):
    def test_unchanged_decision_is_not_delivered_as_an_approval(self):
        from pricepilot.services import telegram_bot
        row = {'id': 53, 'account_id': 11, 'property_id': 11,
               'decision': 'UNCHANGED', 'applied': 0,
               'data_source': 'beds24_observation', 'date': '2026-10-05'}
        with patch('pricepilot.core.database.get_decision_log_entry', return_value=row), \
             patch.object(telegram_bot, 'send_approval_request') as send:
            result = telegram_bot.send_existing_pending_approval(53, 11)
        self.assertEqual(result, {'ok': False, 'error': 'decision_not_pending'})
        send.assert_not_called()

    def test_review_next_ignores_unchanged_and_non_authoritative_pending_rows(self):
        from pricepilot.services import telegram_bot
        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).isoformat()
        current = {'id': 52, 'account_id': 11, 'property_id': 11,
                   'decision': 'PENDING_APPROVAL: 89.00->93.45 (+5.0%)',
                   'timestamp': stamp, 'date': '2026-10-05',
                   'old_price': 89.0, 'new_price': 93.45}
        unchanged = {**current, 'id': 53, 'date': '2026-10-06',
                     'decision': 'UNCHANGED'}
        superseded = {**current, 'id': 54, 'date': '2026-10-07'}

        def calendar(_property_id, target_date, _account_id):
            if target_date == current['date']:
                return {'decision_log_id': 52, 'status': 'pending_approval'}
            if target_date == superseded['date']:
                return {'decision_log_id': 55, 'status': 'pending_approval'}
            return {'decision_log_id': 53, 'status': 'unchanged'}

        with patch.object(telegram_bot, '_decision_context_for_chat', return_value={
                'account_id': 11, 'property_id': 11}), \
             patch('pricepilot.core.database.get_decision_log_entry', return_value=current), \
             patch('pricepilot.core.database.get_decision_log', return_value=[
                 current, unchanged, superseded]), \
             patch('pricepilot.core.database.get_calendar_price', side_effect=calendar), \
             patch('pricepilot.engine.decision_engine._scoped_property', return_value={
                 'id': 11, 'account_id': 11, 'name': 'Luma Pisa'}), \
             patch.object(telegram_bot, 'send_approval_request', return_value={
                 'ok': True, 'result': {'message_id': 9}}) as send:
            self.assertTrue(telegram_bot._review_pending(52, 44))
        self.assertIsNone(send.call_args.args[10])

    def test_existing_pending_approval_is_delivered_once_and_returns_message_id(self):
        from pricepilot.services import telegram_bot
        row = {'id': 52, 'account_id': 11, 'property_id': 11,
               'decision': 'PENDING_APPROVAL: 89.00->93.45 (+5.0%)',
               'applied': 0, 'data_source': 'beds24_observation',
               'old_price': 89.0, 'new_price': 93.45, 'occupancy': .2,
               'market_avg': None, 'date': '2026-10-04', 'notes': 'calendar rule',
               'factors': json.dumps({'lead_time_band': 'URGENT',
                                      'hours_until_checkin': 19.5})}
        with patch('pricepilot.core.database.get_decision_log_entry', return_value=row), \
             patch('pricepilot.core.database.get_calendar_price', return_value={
                 'decision_log_id': 52, 'status': 'pending_approval'}), \
             patch('pricepilot.core.database.get_property', return_value={'id': 11, 'account_id': 11, 'name': 'Luma Pisa'}), \
             patch('pricepilot.core.database.get_telegram_link_by_property', return_value={'chat_id': 44}), \
             patch('pricepilot.core.database.get_notification_preferences', return_value={'telegram_enabled': 1, 'approval_alerts': 1}), \
             patch.object(telegram_bot, 'send_approval_request', return_value={'ok': True, 'result': {'message_id': 9001}}) as send, \
             patch('pricepilot.core.database.record_notification_log'):
            result = telegram_bot.send_existing_pending_approval(52, 11)
        self.assertEqual(result, {'ok': True, 'message_id': '9001', 'log_id': 52})
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs['decision_factors'],
                         {'lead_time_band': 'URGENT', 'hours_until_checkin': 19.5})

    def test_existing_pending_approval_with_message_id_is_not_resent(self):
        from pricepilot.services import telegram_bot
        row = {'id': 52, 'account_id': 11, 'property_id': 11,
               'decision': 'PENDING_APPROVAL: 89.00->93.45 (+5.0%)',
               'applied': 0, 'data_source': 'beds24_observation', 'tg_message_id': 9001}
        with patch('pricepilot.core.database.get_decision_log_entry', return_value=row), \
             patch.object(telegram_bot, 'send_approval_request') as send:
            result = telegram_bot.send_existing_pending_approval(52, 11)
        self.assertEqual(result, {'ok': True, 'already_sent': True, 'message_id': '9001'})
        send.assert_not_called()

    def test_approval_send_requires_telegram_message_id(self):
        from pricepilot.services import telegram_bot
        with patch.object(telegram_bot, '_api_call', return_value={'ok': True, 'result': {}}), \
             patch('pricepilot.core.database.update_decision_tg_message') as update:
            result = telegram_bot.send_approval_request(52, 'Luma Pisa', 89, 93.45, .2, None, '', 44,
                                                        'calendar rule', '2026-10-04')
        self.assertTrue(result['ok'])
        update.assert_not_called()

    def test_sandbox_approval_has_distinct_callbacks_and_no_operational_writer(self):
        from pricepilot.services import telegram_bot
        with patch.object(telegram_bot, '_api_call', return_value={'ok': True}) as send:
            telegram_bot.send_test_approval_request(42, 'Luma Pisa', '2099-01-02', 89, 91, 44)
        payload = send.call_args.args[1]
        self.assertIn('TEST TECNICO SANDBOX', payload['text'])
        self.assertIn('non è una raccomandazione reale', payload['text'])
        buttons = payload['reply_markup']['inline_keyboard'][0]
        self.assertEqual(buttons[0]['callback_data'], 'test_approve_42')
        self.assertEqual(buttons[1]['callback_data'], 'test_reject_42')

    def test_sandbox_approval_never_calls_channel_writer(self):
        from pricepilot.services import telegram_bot
        from datetime import datetime, timezone
        row = {'id': 42, 'account_id': 3, 'property_id': 7, 'data_source': 'test_sandbox',
               'decision': 'TEST_PENDING_APPROVAL', 'timestamp': datetime.now(timezone.utc).isoformat(),
               'date': '2099-01-02', 'old_price': 89.0, 'new_price': 91.0}
        snap = {'inventory': [{'date': '2099-01-02', 'state': 'open', 'booking_id': None,
                               'arrival_restriction': 'none', 'current_price': 89.0}]}
        with patch.object(telegram_bot, '_decision_context_for_chat', return_value={'id': 42, 'account_id': 3, 'property_id': 7}), \
             patch('pricepilot.core.database.get_decision_log_entry', return_value=row), \
             patch('pricepilot.services.operational_store.get_snapshot', return_value=snap), \
             patch('pricepilot.core.database.update_decision_state') as update, \
             patch.object(telegram_bot, '_record_approval_event'), \
             patch.object(telegram_bot, 'answer_callback_query'), \
             patch.object(telegram_bot, 'edit_message_text'), \
             patch('pricepilot.engine.decision_engine.approve_decision', side_effect=AssertionError('writer path used')):
            telegram_bot._handle_callback('cb', 'test_approve_42', 44, 55, 'TEST')
        self.assertEqual(update.call_args.kwargs['decision'], 'TEST_APPROVED_WRITE_GATE_BLOCKED')

    def test_sandbox_rows_are_excluded_from_operational_digest(self):
        from pricepilot.services import telegram_bot
        with patch.object(telegram_bot, 'is_configured', return_value=True), \
             patch.object(telegram_bot, '_api_call') as send:
            result = telegram_bot.send_cycle_digest(3, [{'property_id': 7, 'data_source': 'test_sandbox',
                'mode': 'approval', 'calendar_status': 'pending_approval', 'date': '2099-01-02'}])
        self.assertEqual(result, {'sent': 0, 'failed': 0})
        send.assert_not_called()

    def test_ninety_nights_produce_one_digest_and_one_review_entry(self):
        from pricepilot.services import telegram_bot
        rows = [{'property_id': 7, 'property_name': 'Luma',
                 'date': (date.today()+timedelta(days=i)).isoformat(),
                 'old_price': 100, 'recommended_price': 90,
                 'mode': 'approval', 'calendar_status': 'pending_approval',
                 'log_id': i+1, 'breakdown': {}} for i in range(90)]
        with patch.object(telegram_bot, 'is_configured', return_value=True), \
             patch.object(telegram_bot, '_api_call', return_value={'ok': True, 'result': {'message_id': 9}}) as send, \
             patch.object(telegram_bot, 'send_existing_pending_approval', return_value={'ok': True, 'message_id': '10'}) as deliver, \
             patch('pricepilot.core.database.get_property', return_value={'id': 7, 'account_id': 3}), \
             patch('pricepilot.core.database.get_telegram_link_by_property', return_value={'chat_id': 44}), \
             patch('pricepilot.core.database.get_notification_preferences', return_value={'telegram_enabled': 1, 'approval_alerts': 1}), \
             patch('pricepilot.core.database.record_notification_log'):
            outcome = telegram_bot.send_cycle_digest(3, rows)
        self.assertEqual(outcome['sent'], 1)
        self.assertEqual(send.call_count, 1)
        deliver.assert_called_once_with(1, 3)
        payload = send.call_args.args[1]
        self.assertIn('90 proposte', payload['text'])
        self.assertEqual(payload['reply_markup']['inline_keyboard'][0][0]['callback_data'], 'review_1')

    def test_manual_minimum_stay_review_has_no_price_approval_button(self):
        from pricepilot.services import telegram_bot
        rows = [{'property_id': 7, 'property_name': 'Luma', 'date': date.today().isoformat(),
                 'old_price': 100, 'recommended_price': 100, 'mode': 'approval',
                 'calendar_status': 'manual_review', 'log_id': 1,
                 'breakdown': {'manual_actions': [{'current_minimum_stay': 3,
                     'suggested_minimum_stay': 2}]}}]
        with patch.object(telegram_bot, 'is_configured', return_value=True), \
             patch.object(telegram_bot, '_api_call', return_value={'ok': True}) as send, \
             patch('pricepilot.core.database.get_property', return_value={'id': 7, 'account_id': 3}), \
             patch('pricepilot.core.database.get_telegram_link_by_property', return_value={'chat_id': 44}), \
             patch('pricepilot.core.database.get_notification_preferences', return_value={'telegram_enabled': 1, 'approval_alerts': 1}), \
             patch('pricepilot.core.database.record_notification_log'):
            telegram_bot.send_cycle_digest(3, rows)
        payload = send.call_args.args[1]
        self.assertIn('soggiorno minimo 3 → 2', payload['text'])
        self.assertNotIn('reply_markup', payload)

    def test_stale_callback_marks_original_message_not_applied(self):
        from pricepilot.services import telegram_bot
        with patch.object(telegram_bot, '_decision_context_for_chat', return_value={
                'id': 8, 'account_id': 3, 'property_id': 7}), \
             patch('pricepilot.engine.decision_engine.approve_decision', return_value={
                'approved': False, 'applied': False, 'status': 'stale',
                'message': 'Prezzo cambiato: ricalcolare.'}), \
             patch.object(telegram_bot, '_record_approval_event'), \
             patch.object(telegram_bot, 'answer_callback_query'), \
             patch.object(telegram_bot, 'edit_message_text') as edit:
            telegram_bot._handle_callback('cb', 'approve_8', 44, 55, 'Proposta')
        self.assertIn('*NON APPLICATO*', edit.call_args.args[2])
        self.assertIn('ricalcolare', edit.call_args.args[2])
