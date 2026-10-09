import json
import unittest
from contextlib import ExitStack
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch, Mock

from pricepilot.services import scheduler_scope
from pricepilot.core.scheduler import run_cloud_pricing_cycle


class ProductionScopeTests(unittest.TestCase):
    def setUp(self):
        self.entry = dict(account_id=4, property_id=11, beds24_property_id=357389, room_id=736801)
        self.prop = dict(id=11, account_id=4, name='Luma Pisa')
        self.mapping = dict(self.entry, provider='beds24', enabled=True)

    def resolve(self, entries=None, prop=None, mapping=None, policy=None):
        with patch.object(scheduler_scope, 'SCOPE_FILE') as file, \
                patch('pricepilot.core.database.get_property', return_value=self.prop if prop is None else prop) as lookup, \
                patch('pricepilot.services.operational_store.get_connection', return_value=self.mapping if mapping is None else mapping), \
                patch('pricepilot.services.operational_store.get_calendar_policy', return_value={'enabled': True} if policy is None else policy), \
                patch('pricepilot.core.database.get_properties') as enumerate_all:
            file.read_text.return_value = json.dumps({'properties': [self.entry] if entries is None else entries})
            result = scheduler_scope.scheduled_properties()
            enumerate_all.assert_not_called()
            lookup.assert_called_once_with(11, account_id=4)
            return result

    def test_only_luma_is_resolved_without_reading_secure_apt(self):
        self.assertEqual(self.resolve(), {4: [self.prop]})

    def test_production_job_runs_scoped_engine_with_read_only_runtime(self):
        from pathlib import Path
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/pricing-scheduler.yml').read_text(encoding='utf-8')
        for required in ('PRICEPILOT_DATABASE_BACKEND: supabase',
                         'PRICEPILOT_OCCUPANCY_PROVIDER: observed_inventory',
                         'PRICEPILOT_CHANNEL_PROVIDER: beds24',
                         'PRICEPILOT_OPERATIONAL_MODE: approval',
                         "PRICEPILOT_ALLOW_CHANNEL_WRITES: '0'",
                         'secrets.BEDS24_LUMA_REFRESH_TOKEN',
                         "run_cloud_pricing_cycle(source='github_actions')"):
            self.assertIn(required, workflow)
        self.assertNotIn('process_telegram_update', workflow)
        self.assertNotIn('set_price(', workflow)

    def test_mapping_or_account_mismatch_fails_closed(self):
        for mapping in (dict(self.mapping, room_id=1), dict(self.mapping, beds24_property_id=1),
                        dict(self.mapping, enabled=False), dict(self.mapping, account_id=2)):
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                self.resolve(mapping=mapping)
        with self.assertRaises(ValueError):
            self.resolve(prop=dict(self.prop, account_id=2))
        with self.assertRaises(ValueError):
            self.resolve(policy={'enabled': False})

    def test_empty_or_duplicate_scope_fails_closed(self):
        for entries in ([], [self.entry, self.entry]):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                self.resolve(entries=entries)

    def test_secure_apt_never_enters_cycle_decisions_notifications_or_writer(self):
        # The automatic dispatcher only hands the certified Luma property to the
        # normal cycle. No account-wide fetch can reintroduce another property.
        with patch('pricepilot.services.scheduler_scope.scheduled_properties', return_value=self.resolve()), \
                patch('pricepilot.core.scheduler.run_pricing_cycle', return_value={'results': [], 'errors': []}) as cycle, \
                patch('pricepilot.core.database.get_properties') as unscoped:
            result = run_cloud_pricing_cycle()
        cycle.assert_called_once()
        self.assertEqual(cycle.call_args.kwargs['account_id'], 4)
        self.assertEqual(cycle.call_args.kwargs['_scheduled_properties'], [self.prop])
        unscoped.assert_not_called()
        self.assertEqual(result['accounts_processed'], 1)

    def test_invalid_scope_prevents_all_cycles(self):
        with patch('pricepilot.services.scheduler_scope.scheduled_properties', side_effect=ValueError('invalid')), \
                patch('pricepilot.core.scheduler.run_pricing_cycle') as cycle:
            with self.assertRaises(ValueError):
                run_cloud_pricing_cycle()
        cycle.assert_not_called()

    def test_real_cycle_only_calls_providers_engine_and_sender_for_luma(self):
        from pricepilot.core.scheduler import run_pricing_cycle
        from pricepilot.core import database
        provider = Mock()
        provider.name = 'fixture_inventory'
        provider.estimate.return_value = SimpleNamespace(occupancy=.3, source='fixture', raw={'target_state': 'open'})
        with ExitStack() as stack:
            for name, value in {'get_account': {'plan': 'plus', 'billing_status': 'dev'},
                                'try_start_operation_run': (1, None), 'finish_operation_run': {'id': 1},
                                'record_audit_event': None}.items():
                stack.enter_context(patch.object(database, name, return_value=value))
            unscoped = stack.enter_context(patch.object(database, 'get_properties', return_value=[self.prop, {'id': 1, 'account_id': 2, 'name': 'Secure Apt'}]))
            stack.enter_context(patch('pricepilot.providers.registry.get_occupancy_provider', return_value=provider))
            engine = stack.enter_context(patch('pricepilot.engine.decision_engine.process_decision', return_value={'mode': 'approval'}))
            sender = stack.enter_context(patch('pricepilot.services.telegram_bot.send_cycle_digest', return_value={}))
            writer = stack.enter_context(patch('pricepilot.integrations.beds24.Beds24Client.set_price'))
            run_pricing_cycle(account_id=4, target_date=date(2030, 1, 1), source='github_actions', _scheduled_properties=[self.prop])
        unscoped.assert_not_called()
        provider.estimate.assert_called_once_with(property_id=11, target_date=date(2030, 1, 1), account_id=4)
        self.assertEqual(engine.call_args.kwargs['property_id'], 11)
        self.assertEqual(engine.call_args.kwargs['account_id'], 4)
        self.assertEqual(sender.call_args.args[0], 4)
        writer.assert_not_called()

    def test_future_explicit_multi_property_scope_keeps_tenant_binding(self):
        second = dict(self.entry, account_id=7, property_id=22, beds24_property_id=100, room_id=200)
        entries = [self.entry, second]
        with patch.object(scheduler_scope, 'SCOPE_FILE') as file, \
                patch('pricepilot.core.database.get_property', side_effect=lambda pid, account_id: {'id': pid, 'account_id': account_id}), \
                patch('pricepilot.services.operational_store.get_connection', side_effect=lambda aid, pid: dict(next(e for e in entries if e['account_id'] == aid), provider='beds24', enabled=True)), \
                patch('pricepilot.services.operational_store.get_calendar_policy', return_value={'enabled': True}):
            file.read_text.return_value = json.dumps({'properties': entries})
            selected = scheduler_scope.scheduled_properties()
        self.assertEqual(set(selected), {4, 7})
        self.assertEqual(selected[7][0]['id'], 22)
