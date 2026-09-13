"""Dashboard setup and readiness tests; no network or real credentials."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from pricepilot.core import database as db
from pricepilot.core.config import CONFIG
from pricepilot.dashboard import auth
from pricepilot.dashboard.setup import default_policy
from pricepilot.engine.calendar_pricing import validate_policy
from pricepilot.services.operational_store import save_calendar_policy, save_connection, save_snapshot
from pricepilot.services.property_service import create_property
from pricepilot.services.readiness import property_readiness


class DashboardSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        previous = CONFIG['db_path']
        CONFIG['db_path'] = str(Path(self.tmp.name) / 'dashboard-setup.db')
        self.addCleanup(lambda: CONFIG.update(db_path=previous))
        db.init_db()
        self.account = db.create_account('UI fixture', plan='plus')['id']
        self.foreign = db.create_account('Foreign UI fixture', plan='plus')['id']
        self.prop = create_property({
            'account_id': self.account, 'name': 'Luma fixture', 'city': 'Pisa',
            'min_price': 50, 'max_price': 300, 'platform': 'airbnb',
            'plan': 'plus', 'sync_mode': 'approval',
        })

    def test_neutral_ui_policy_is_valid_and_does_not_enable_discounts(self):
        policy = default_policy(100)
        validate_policy(policy)
        self.assertFalse(policy['enabled'])
        self.assertTrue(all(b['low_multiplier'] == 1 for b in policy['lead_time_bands']))
        self.assertFalse(policy['gap_rule']['enabled'])

    def test_readiness_has_separate_evidence_gates(self):
        pid = self.prop['id']
        policy = default_policy(100)
        save_calendar_policy(self.account, pid, {**policy, 'enabled': True})
        save_connection(self.account, pid, {
            'provider': 'beds24', 'enabled': True, 'beds24_property_id': 10,
            'room_id': 20, 'price_slot': 1, 'currency': 'EUR',
            'token_env': 'BEDS24_TEST_TOKEN', 'refresh_token_env': '',
            'price_basis': 'unknown',
        })
        start = date.today()
        observed = datetime.now(timezone.utc).isoformat()
        inventory = [{
            'account_id': self.account, 'property_id': pid,
            'date': (start + timedelta(days=i)).isoformat(), 'state': 'open',
            'observed_at': observed,
        } for i in range(30)]
        save_snapshot(self.account, pid, inventory, [], start, start + timedelta(days=30), observed)
        with patch.dict(os.environ, {'BEDS24_TEST_TOKEN': 'fixture-token',
                                     'PRICEPILOT_ALLOW_CHANNEL_WRITES': '0'}), \
             patch('pricepilot.services.telegram_bot.is_configured', return_value=False):
            status = property_readiness(self.account, pid)
        self.assertTrue(status['configured'])
        self.assertTrue(status['analysis_ready'], status)
        self.assertFalse(status['approval_ready'])
        self.assertFalse(status['write_ready'])
        self.assertFalse(status['ready'])

    def test_readiness_rejects_foreign_property(self):
        with self.assertRaises(ValueError):
            property_readiness(self.foreign, self.prop['id'])


class LiveAuthenticationTests(unittest.TestCase):
    def _st(self, session_state):
        return SimpleNamespace(session_state=session_state,
            sidebar=SimpleNamespace(error=Mock(), warning=Mock()))

    def test_every_live_environment_rejects_local_session(self):
        for environment in ('staging', 'prod', 'production', 'live'):
            with self.subTest(environment=environment), \
                 patch.dict(os.environ, {'PRICEPILOT_ENV': environment,
                                          'PRICEPILOT_AUTH_MODE': 'local'}), \
                 patch.object(auth, 'st', self._st({auth._KEY_USER: {'id': 1}})), \
                 patch.object(auth, '_get_client', return_value=object()), \
                 patch.object(auth, '_render_auth_page') as render:
                self.assertFalse(auth.require_auth())
                render.assert_called_once()

    def test_live_supabase_session_is_accepted(self):
        state = {auth._KEY_USER: {'id': 1}, auth._KEY_SESSION: object()}
        with patch.dict(os.environ, {'PRICEPILOT_ENV': 'production',
                                     'PRICEPILOT_AUTH_MODE': 'local'}), \
             patch.object(auth, 'st', self._st(state)), \
             patch.object(auth, '_get_client', return_value=object()), \
             patch.object(auth, '_emit_pending_auth_cookie'):
            self.assertTrue(auth.require_auth())
