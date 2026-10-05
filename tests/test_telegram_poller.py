import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pricepilot.core.config import CONFIG
from pricepilot.core.database import (
    advance_telegram_update_cursor,
    get_telegram_update_cursor,
    init_db,
)
from pricepilot.services import telegram_bot, telegram_poller


def callback_update(update_id=10, data="approve_155"):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "data": data,
            "message": {
                "message_id": 17,
                "chat": {"id": 12345},
                "text": "PricePilot decision",
            },
        },
    }


class FakeCursor:
    def __init__(self, next_update_id=0):
        self.next_update_id = next_update_id
        self.failures = []

    def get(self, _consumer_key):
        return {"next_update_id": self.next_update_id}

    def advance(self, _consumer_key, **kwargs):
        if kwargs["expected_next_update_id"] != self.next_update_id:
            return False
        self.next_update_id = kwargs["next_update_id"]
        return True

    def fail(self, _consumer_key, **kwargs):
        self.failures.append(kwargs)
        return True


class TelegramPollerTests(unittest.TestCase):
    def run_batch(self, updates, *, cursor=None, handler=None, hook=None, webhook_url=""):
        cursor = cursor or FakeCursor()
        handler = handler or Mock()

        def telegram_call(method, payload):
            if method == "getWebhookInfo":
                return {"ok": True, "result": {"url": webhook_url, "pending_update_count": len(updates)}}
            if method == "getUpdates":
                return {"ok": True, "result": list(updates)}
            raise AssertionError(method)

        with (
            patch.object(telegram_poller, "get_telegram_update_cursor", cursor.get),
            patch.object(telegram_poller, "advance_telegram_update_cursor", cursor.advance),
            patch.object(telegram_poller, "mark_telegram_update_failure", cursor.fail),
        ):
            result = telegram_poller.run_poll_once(
                telegram_call=telegram_call,
                update_handler=handler,
                before_cursor_commit=hook,
            )
        return result, cursor, handler

    def test_get_updates_without_updates_does_nothing(self):
        result, cursor, handler = self.run_batch([])
        self.assertEqual(result["processed"], 0)
        self.assertEqual(cursor.next_update_id, 0)
        handler.assert_not_called()

    def test_approve_update_uses_shared_specific_handler(self):
        update = callback_update(data="approve_155")
        result, cursor, handler = self.run_batch([update])
        handler.assert_called_once_with(update)
        self.assertEqual(result["processed"], 1)
        self.assertEqual(cursor.next_update_id, 11)

    def test_reject_update_uses_shared_handler_without_direct_writer(self):
        update = callback_update(data="reject_155")
        result, _, handler = self.run_batch([update])
        handler.assert_called_once_with(update)
        self.assertEqual(result["processed"], 1)
        source = Path(telegram_poller.__file__).read_text(encoding="utf-8")
        self.assertNotIn("_channel_manager_update", source)

    def test_same_update_on_second_run_is_idempotently_skipped(self):
        cursor = FakeCursor()
        handler = Mock()
        self.run_batch([callback_update()], cursor=cursor, handler=handler)
        result, _, _ = self.run_batch([callback_update()], cursor=cursor, handler=handler)
        self.assertEqual(handler.call_count, 1)
        self.assertEqual(result["duplicates"], 1)

    def test_failure_before_cursor_commit_is_recoverable(self):
        cursor = FakeCursor()
        handler = Mock()
        with self.assertRaisesRegex(RuntimeError, "crash"):
            self.run_batch(
                [callback_update()], cursor=cursor, handler=handler,
                hook=lambda _update: (_ for _ in ()).throw(RuntimeError("crash before cursor commit")),
            )
        self.assertEqual(cursor.next_update_id, 0)
        self.assertEqual(len(cursor.failures), 1)
        self.run_batch([callback_update()], cursor=cursor, handler=handler)
        self.assertEqual(cursor.next_update_id, 11)

    def test_processing_failure_stops_batch_and_keeps_cursor(self):
        cursor = FakeCursor()
        handler = Mock(side_effect=RuntimeError("database unavailable"))
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            self.run_batch([callback_update(), callback_update(11)], cursor=cursor, handler=handler)
        self.assertEqual(handler.call_count, 1)
        self.assertEqual(cursor.next_update_id, 0)
        self.assertEqual(len(cursor.failures), 1)

    def test_malformed_callback_is_terminal_poison_without_handler(self):
        update = callback_update()
        update["callback_query"]["data"] = None
        result, cursor, handler = self.run_batch([update])
        handler.assert_not_called()
        self.assertEqual(result["ignored"], 1)
        self.assertEqual(cursor.next_update_id, 11)

    def test_webhook_conflict_fails_before_get_updates(self):
        with self.assertRaises(telegram_poller.WebhookPollingConflict):
            self.run_batch([callback_update()], webhook_url="https://example.test/telegram/webhook")

    def test_get_updates_error_does_not_advance_cursor(self):
        cursor = FakeCursor()

        def telegram_call(method, payload):
            if method == "getWebhookInfo":
                return {"ok": True, "result": {"url": ""}}
            return {"ok": False, "description": "temporary failure"}

        with (
            patch.object(telegram_poller, "get_telegram_update_cursor", cursor.get),
            patch.object(telegram_poller, "advance_telegram_update_cursor", cursor.advance),
        ):
            with self.assertRaises(telegram_poller.TelegramPollingError):
                telegram_poller.run_poll_once(telegram_call=telegram_call)
        self.assertEqual(cursor.next_update_id, 0)

    def test_http_webhook_wrapper_still_uses_shared_handler(self):
        update = callback_update()
        with patch.object(telegram_bot, "process_telegram_update") as shared:
            telegram_bot.process_webhook(update)
        shared.assert_called_once_with(update)


class TelegramCursorPersistenceTests(unittest.TestCase):
    def test_sqlite_cursor_persists_and_uses_compare_and_swap(self):
        previous = CONFIG["db_path"]
        try:
            with tempfile.TemporaryDirectory(dir=os.getcwd()) as temp_dir:
                CONFIG["db_path"] = str(Path(temp_dir) / "poller.db")
                init_db()
                first = get_telegram_update_cursor("test-consumer")
                self.assertEqual(first["next_update_id"], 0)
                self.assertTrue(advance_telegram_update_cursor(
                    "test-consumer",
                    expected_next_update_id=0,
                    next_update_id=43,
                    last_update_id=42,
                    status="processed",
                ))
                self.assertFalse(advance_telegram_update_cursor(
                    "test-consumer",
                    expected_next_update_id=0,
                    next_update_id=44,
                    last_update_id=43,
                    status="processed",
                ))
                self.assertEqual(get_telegram_update_cursor("test-consumer")["next_update_id"], 43)
        finally:
            CONFIG["db_path"] = previous


if __name__ == "__main__":
    unittest.main()
