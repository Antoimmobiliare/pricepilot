import io
import tempfile
import unittest
from pathlib import Path
from datetime import date
from unittest.mock import patch, Mock
from urllib.error import HTTPError

from pricepilot.services import approval_history as history, supabase_primary as cloud, telegram_bot
from pricepilot.core import database as db
from pricepilot.core.config import CONFIG


class ApprovalHistoryTests(unittest.TestCase):
    prop = {'id': 11, 'account_id': 4}
    row = {'id': 373, 'account_id': 4, 'property_id': 11, 'date': '2026-10-11', 'new_price': 84.38,
           'applied': True, 'decision': 'PENDING_APPROVAL [APPROVED_SYNCED]'}
    channel = {'ok': True, 'is_real': True, 'platform': 'beds24', 'listing_id': '736801',
               'raw': {'confirmation_scope': 'beds24_calendar', 'date': '2026-10-11',
                       'price_slot': 'price1', 'price': '84.38'}}

    def test_cloud_workflow_waits_for_default_horizon_without_retry(self):
        import re
        from pricepilot.core.scheduler import _cycle_timeout_seconds
        root = Path(__file__).resolve().parents[1]
        workflow = (root/'.github/workflows/pricing-scheduler.yml').read_text(encoding='utf-8')
        http_limit = int(re.search(r'--max-time (\d+)', workflow).group(1))
        job_limit = int(re.search(r'timeout-minutes: (\d+)', workflow).group(1))*60
        self.assertGreater(http_limit, _cycle_timeout_seconds(90)+60)
        self.assertGreater(job_limit, http_limit+60)
        self.assertNotIn('--retry', workflow)
        self.assertIn("vars.PRICEPILOT_SCHEDULER_ENABLED == 'true'", workflow)

    def test_reconciliation_idempotent_without_writer(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(CONFIG, db_path=str(Path(temp)/'history.db')):
            first = history.record_confirmed_approval(self.prop, self.row, self.channel, '2026-10-09T12:13:59+00:00')
            second = history.record_confirmed_approval(self.prop, self.row, self.channel, '2026-10-09T12:13:59+00:00')
            self.assertEqual(first, second)
            self.assertEqual(len(db.get_price_updates([11])), 1)

    def test_cloud_primary_key_is_atomic_and_decision_specific(self):
        client = Mock()
        client.table.return_value.upsert.return_value.execute.return_value.data = []
        saved = []
        def lookup(table, filters, limit):
            return [{**saved[-1], 'local_id': 1}]
        def upsert(payload, **kwargs):
            saved.append(payload)
            self.assertEqual(kwargs, {'on_conflict': 'id', 'ignore_duplicates': True})
            return client.table.return_value.upsert.return_value
        client.table.return_value.upsert.side_effect = upsert
        result = {**self.channel, 'decision_log_id': 373, 'new_price': 84.38}
        with patch.object(cloud, '_client', return_value=client), patch.object(cloud, '_select', side_effect=lookup):
            cloud.record_price_update(self.prop, result, date(2026,10,11))
            cloud.record_price_update(self.prop, result, date(2026,10,11))
            cloud.record_price_update(self.prop, {**result, 'decision_log_id': 374}, date(2026,10,11))
        self.assertEqual(saved[0]['id'], saved[1]['id'])
        self.assertNotEqual(saved[0]['id'], saved[2]['id'])

    def test_missing_or_mismatched_readback_is_rejected(self):
        for raw in ({}, {**self.channel['raw'], 'price':'89'}, {**self.channel['raw'], 'date':'2026-10-12'}):
            with patch('pricepilot.core.database.record_price_update') as record:
                with self.assertRaises((ValueError, ArithmeticError)):
                    history.record_confirmed_approval(self.prop, self.row, {**self.channel,'raw':raw}, 'now')
                record.assert_not_called()

    def test_backfill_uses_confirmed_audit_and_no_channel_call(self):
        audit = {'id':968,'entity_id':'373','action':'decision_approved','status':'applied',
                 'timestamp':'2026-10-09T12:13:59+00:00',
                 'details':{'applied':True,'channel_manager':self.channel}}
        with patch('pricepilot.core.database.get_decision_log_entry', return_value=self.row), \
             patch('pricepilot.core.database.get_property', return_value=self.prop), \
             patch('pricepilot.core.database.get_audit_events', return_value=[audit]), \
             patch('pricepilot.core.database.record_audit_event') as trail, \
             patch('pricepilot.core.database.record_price_update', return_value=1), \
             patch('pricepilot.engine.decision_engine._channel_manager_update') as writer:
            self.assertEqual(history.reconcile_approval_history(373,4),1)
        writer.assert_not_called()
        self.assertFalse(trail.call_args.kwargs['details']['channel_write_performed'])

    def test_expired_ack_is_nonblocking_but_other_400_is_error(self):
        for description, expected in [('query is too old and response timeout expired',True),('chat not found',False)]:
            exc=HTTPError('https://telegram.invalid',400,'Bad Request',{},io.BytesIO(description.encode()))
            with patch.object(telegram_bot,'get_bot_token',return_value='test'), \
                 patch('urllib.request.urlopen',side_effect=exc):
                result=telegram_bot.answer_callback_query('real-query')
            self.assertFalse(result['ok'])
            self.assertEqual(result.get('non_blocking',False),expected)

    def test_expired_ack_does_not_prevent_message_update_or_repeat_write(self):
        with patch.object(telegram_bot,'_decision_context_for_chat',return_value={'account_id':4}), \
             patch('pricepilot.engine.decision_engine.approve_decision',return_value={'approved':True,'applied':True,'status':'applied'}) as approve, \
             patch.object(telegram_bot,'_record_approval_event'), \
             patch.object(telegram_bot,'answer_callback_query',return_value={'ok':False,'non_blocking':True}), \
             patch.object(telegram_bot,'edit_message_text',return_value={'ok':True}) as edit:
            telegram_bot._handle_callback('query','approve_373',123,23,'Proposal')
        approve.assert_called_once()
        edit.assert_called_once()
