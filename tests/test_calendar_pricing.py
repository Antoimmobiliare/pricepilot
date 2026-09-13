"""Own-calendar workflow: no market access, mandatory human approval."""
from datetime import date, timedelta
from copy import deepcopy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

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
        self.assertEqual(high['recommended_price'], 108)
        self.assertEqual(low['breakdown']['unbounded_reference_target'], 90)

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

    def test_rejected_proposal_is_reconsidered_after_material_context_change(self):
        prior = {'policy_fingerprint': 'x', 'current_price_source': 'beds24_observation',
                 'reference_price': 100, 'occupancy_multiplier': .9,
                 'pacing_multiplier': 1, 'pickup_7d_nights': 0,
                 'weekend_multiplier': 1, 'lead_time_days': 3,
                 'gap_multiplier': 1, 'gap_nights': None,
                 'effective_multiplier': .9, 'manual_actions': []}
        row = {'old_price': 100, 'new_price': 90, 'mode': 'approval',
               'decision': 'PENDING_APPROVAL [REJECTED]', 'factors': json.dumps(prior)}
        self.assertTrue(engine._reuse_proposal(row, 100, 90, prior, 'approval'))
        changed = {**prior, 'lead_time_days': 2}
        self.assertFalse(engine._reuse_proposal(row, 100, 90, changed, 'approval'))

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


class CalendarWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory())
        old_db = CONFIG['db_path']
        CONFIG['db_path'] = str(Path(temporary)/'calendar.db')
        self.stack.callback(lambda: CONFIG.update(db_path=old_db))
        self.stack.enter_context(patch.dict(os.environ, {'PRICEPILOT_ENV': 'staging',
            'PRICEPILOT_DATA_PROVIDER': 'unconfigured', 'PRICEPILOT_DATABASE_BACKEND': 'sqlite'}))
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

    def test_identical_pending_and_rejected_proposals_are_not_resent(self):
        first = self.run_decision()
        second = self.run_decision()
        self.assertTrue(second['deduplicated'])
        self.assertEqual(second['log_id'], first['log_id'])
        self.assertEqual(self.telegram.call_count, 1)
        db.mark_decision_rejected(first['log_id'], self.account_id)
        third = self.run_decision()
        self.assertTrue(third['deduplicated'])
        self.assertEqual(third['log_id'], first['log_id'])
        self.assertEqual(self.telegram.call_count, 1)

    def test_changed_price_source_blocks_approval_even_when_amount_matches(self):
        result = self.run_decision()
        db.upsert_calendar_price({**self.calendar, 'current_price_source': 'manual',
                                  'decision_log_id': result['log_id'],
                                  'recommended_price': result['recommended_price'],
                                  'status': 'pending_approval'})
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
        self.assertIn('competitor da verificare manualmente', body['text'])
        self.assertEqual(len(body['reply_markup']['inline_keyboard'][0]), 2)

    def test_readiness_does_not_require_external_market_source(self):
        with patch.dict(os.environ, {'PRICEPILOT_ENV': 'production'}), \
             patch.object(server, 'get_market_data_provider', side_effect=AssertionError('No market access')), \
             patch.object(server, 'get_event_provider', side_effect=AssertionError('No event access')), \
             patch.object(server, 'get_occupancy_provider', return_value=SimpleNamespace(name='observed_inventory')):
            checks = server._deployment_readiness_checks()
        self.assertTrue(checks['data_providers']['ok'])


class TelegramCalendarWorkflowTests(unittest.TestCase):
    def test_ninety_nights_produce_one_digest_and_one_review_entry(self):
        from pricepilot.services import telegram_bot
        rows = [{'property_id': 7, 'property_name': 'Luma',
                 'date': (date.today()+timedelta(days=i)).isoformat(),
                 'old_price': 100, 'recommended_price': 90,
                 'mode': 'approval', 'calendar_status': 'pending_approval',
                 'log_id': i+1, 'breakdown': {}} for i in range(90)]
        with patch.object(telegram_bot, 'is_configured', return_value=True), \
             patch.object(telegram_bot, '_api_call', return_value={'ok': True}) as send, \
             patch('pricepilot.core.database.get_property', return_value={'id': 7, 'account_id': 3}), \
             patch('pricepilot.core.database.get_telegram_link_by_property', return_value={'chat_id': 44}), \
             patch('pricepilot.core.database.get_notification_preferences', return_value={'telegram_enabled': 1, 'approval_alerts': 1}), \
             patch('pricepilot.core.database.record_notification_log'):
            outcome = telegram_bot.send_cycle_digest(3, rows)
        self.assertEqual(outcome['sent'], 1)
        self.assertEqual(send.call_count, 1)
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
