import unittest
from unittest.mock import patch


def callback_update(data="approve_155"):
    return {
        "update_id": 9001,
        "callback_query": {
            "id": "callback-155",
            "data": data,
            "message": {
                "chat": {"id": 577177352},
                "message_id": 17,
                "text": "Proposta PricePilot",
            },
        },
    }


class TelegramWebhookReliabilityTests(unittest.TestCase):
    def test_callback_is_audited_before_single_handler_invocation(self):
        from pricepilot.services import telegram_bot

        with patch.object(telegram_bot, "_record_callback_audit") as audit, \
             patch.object(telegram_bot, "_handle_callback") as handler:
            telegram_bot.process_webhook(callback_update())

        handler.assert_called_once_with(
            "callback-155", "approve_155", 577177352, 17, "Proposta PricePilot"
        )
        audit.assert_called_once()
        kwargs = audit.call_args.kwargs
        self.assertEqual(kwargs["status"], "received")
        self.assertEqual(kwargs["callback_query_id"], "callback-155")
        self.assertEqual(kwargs["message_id"], 17)
        self.assertEqual(kwargs["callback_data"], "approve_155")
        self.assertEqual(kwargs["decision_log_id"], 155)

    def test_processing_exception_is_observable_and_not_swallowed(self):
        from pricepilot.services import telegram_bot

        with patch.object(telegram_bot, "_record_callback_audit") as audit, \
             patch.object(telegram_bot, "_handle_callback", side_effect=RuntimeError("writer unavailable")), \
             patch.object(telegram_bot, "answer_callback_query", return_value={"ok": True}) as answer, \
             patch("pricepilot.engine.decision_engine._channel_manager_update") as writer:
            with self.assertRaisesRegex(RuntimeError, "writer unavailable"):
                telegram_bot.process_webhook(callback_update())

        self.assertEqual([call.kwargs["status"] for call in audit.call_args_list], ["received", "error"])
        self.assertIn("RuntimeError", audit.call_args_list[-1].kwargs["error"])
        answer.assert_called_once_with(
            "callback-155", "PricePilot: errore interno, nessuna modifica applicata."
        )
        writer.assert_not_called()

    def test_error_ack_failure_does_not_turn_callback_into_success(self):
        from pricepilot.services import telegram_bot

        with patch.object(telegram_bot, "_record_callback_audit"), \
             patch.object(telegram_bot, "_handle_callback", side_effect=ValueError("invalid state")), \
             patch.object(telegram_bot, "answer_callback_query", side_effect=OSError("telegram down")):
            with self.assertRaisesRegex(ValueError, "invalid state"):
                telegram_bot.process_webhook(callback_update())

if __name__ == "__main__":
    unittest.main()
