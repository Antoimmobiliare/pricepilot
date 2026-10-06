from contextlib import ExitStack
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from pricepilot.services import telegram_bot, telegram_poller
from pricepilot.core import database


class TelegramReviewDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.row = dict(id=42, account_id=4, property_id=11, date='2026-10-11',
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        old_price=89, new_price=93.45, applied=False,
                        decision='PENDING_APPROVAL', tg_message_id='', factors={})
        self.pointer = dict(decision_log_id=42, status='pending_approval')

    def dependencies(self, stack):
        stack.enter_context(patch.object(telegram_bot, '_decision_context_for_chat',
                           return_value=dict(id=42, account_id=4, property_id=11)))
        stack.enter_context(patch.object(database, 'get_decision_log_entry', return_value=self.row))
        stack.enter_context(patch.object(database, 'get_calendar_price', return_value=self.pointer))
        stack.enter_context(patch.object(database, 'get_decision_log', return_value=[]))
        stack.enter_context(patch('pricepilot.engine.decision_engine._scoped_property',
                           return_value=dict(name='Example property')))

    def test_polled_review_reaches_shared_handler_and_persists_specific_buttons(self):
        update = dict(update_id=10, callback_query=dict(id='review-callback', data='review_42',
                      message=dict(message_id=70, chat=dict(id=123), text='Overview')))
        with ExitStack() as stack:
            self.dependencies(stack)
            stack.enter_context(patch.object(telegram_bot, '_record_callback_audit'))
            stack.enter_context(patch.object(telegram_poller, 'get_telegram_update_cursor',
                               return_value=dict(next_update_id=0)))
            advance = stack.enter_context(patch.object(telegram_poller, 'advance_telegram_update_cursor', return_value=True))
            persist = stack.enter_context(patch.object(database, 'update_decision_tg_message'))
            api = stack.enter_context(patch.object(telegram_bot, '_api_call',
                                      return_value=dict(ok=True, result=dict(message_id=71))))
            def transport(method, payload):
                return dict(ok=True, result=[update] if method == 'getUpdates' else dict(url=''))
            outcome = telegram_poller.run_poll_once(telegram_call=transport)
            self.assertEqual(outcome['processed'], 1)
            persist.assert_called_once_with(42, 71)
            sent = next(c.args[1] for c in api.call_args_list if c.args[0] == 'sendMessage')
            callbacks = [b['callback_data'] for row in sent['reply_markup']['inline_keyboard'] for b in row]
            self.assertIn('approve_42', callbacks)
            self.assertIn('reject_42', callbacks)
            advance.assert_called_once()

    def test_failed_review_delivery_does_not_claim_success_or_advance_cursor(self):
        with ExitStack() as stack:
            self.dependencies(stack)
            stack.enter_context(patch.object(telegram_bot, '_record_callback_audit'))
            stack.enter_context(patch.object(telegram_bot, '_api_call', return_value=dict(ok=False)))
            stack.enter_context(patch.object(telegram_poller, 'get_telegram_update_cursor', return_value=dict(next_update_id=0)))
            advance = stack.enter_context(patch.object(telegram_poller, 'advance_telegram_update_cursor'))
            failure = stack.enter_context(patch.object(telegram_poller, 'mark_telegram_update_failure'))
            u = dict(update_id=10, callback_query=dict(id='review', data='review_42',
                     message=dict(message_id=70, chat=dict(id=123), text='Overview')))
            def transport(method, payload):
                return dict(ok=True, result=[u] if method == 'getUpdates' else dict(url=''))
            with self.assertRaisesRegex(RuntimeError, 'consegna'):
                telegram_poller.run_poll_once(telegram_call=transport)
            advance.assert_not_called()
            failure.assert_called_once()

    def test_review_already_delivered_does_not_resend(self):
        self.row['tg_message_id'] = '71'
        with ExitStack() as stack:
            self.dependencies(stack)
            api = stack.enter_context(patch.object(telegram_bot, '_api_call'))
            self.assertTrue(telegram_bot._review_pending(42, 123))
            api.assert_not_called()

    def test_review_wrong_calendar_pointer_never_sends(self):
        self.pointer['decision_log_id'] = 99
        with ExitStack() as stack:
            self.dependencies(stack)
            api = stack.enter_context(patch.object(telegram_bot, '_api_call'))
            self.assertFalse(telegram_bot._review_pending(42, 123))
            api.assert_not_called()

    def test_digest_persists_message_and_delivers_first_decision(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(telegram_bot, 'is_configured', return_value=True))
            stack.enter_context(patch.object(database, 'get_property', return_value=dict(name='Example')))
            stack.enter_context(patch.object(database, 'get_notification_preferences', return_value={}))
            stack.enter_context(patch.object(database, 'get_telegram_link_by_property', return_value=dict(chat_id=123)))
            record = stack.enter_context(patch.object(database, 'record_notification_log'))
            stack.enter_context(patch.object(telegram_bot, '_api_call', return_value=dict(ok=True, result=dict(message_id=70))))
            deliver = stack.enter_context(patch.object(telegram_bot, 'send_existing_pending_approval', return_value=dict(ok=True, message_id='71')))
            rows = [dict(log_id=n, property_id=11, property_name='Example', date=f'2026-10-{n-30}',
                         mode='approval', calendar_status='pending_approval', old_price=89,
                         recommended_price=93.45) for n in [42, 41]]
            self.assertEqual(telegram_bot.send_cycle_digest(4, rows), dict(sent=1, failed=0))
            record.assert_called_once()
            self.assertEqual(record.call_args.kwargs['message_id'], '70')
            deliver.assert_called_once_with(41, 4)


if __name__ == '__main__':
    unittest.main()
