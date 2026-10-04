from copy import deepcopy
import unittest
from unittest.mock import patch

from pricepilot.services import telegram_bot


def factors(**changes):
    value = {
        "hours_until_checkin": 19 + 32 / 60,
        "lead_time_band": "URGENT",
        "pickup_7d_nights": 0,
        "gap_nights": None,
        "unsold_risk_pressure": .42,
        "urgency_action": "amplify_negative_signals",
        "signal_conflict": False,
        "negative_signals": [
            {"signal": "occupancy_weak"}, {"signal": "pickup_weak"},
        ],
        "positive_signals": [],
        "rules_confidence": "supported",
    }
    value.update(changes)
    return value


def payload(*, old=89, new=86, occupancy=.2, detail=None, reason="Regole calendario applicate.", market=None):
    return telegram_bot._build_approval_payload(
        log_id=42, prop_name="Luma Pisa", old_price=old, new_price=new,
        occupancy=occupancy, market_avg=market, event="", reason=reason,
        target_date="2026-10-06", decision_factors=detail or factors(),
    )


class TelegramDecisionUxTests(unittest.TestCase):
    def test_urgent_reduction_has_mobile_first_order_and_real_signals(self):
        text = payload()["text"]
        expected = ["🏠 *Luma Pisa*", "📅 6 ottobre 2026", "Check-in tra 19h 32m · URGENT",
                    "€89,00 → €86,00", "Variazione: -€3,00 / -3,4%", "📊 *Segnali*",
                    "Occupancy: 20%", "Pickup 7gg: 0 notti", "Rischio invenduto: elevato",
                    "💡 *PricePilot*", "Regole calendario applicate."]
        positions = [text.index(item) for item in expected]
        self.assertEqual(positions, sorted(positions))

    def test_increase_uses_neutral_copy_and_positive_signals(self):
        detail = factors(unsold_risk_pressure=0, urgency_action="hold_positive_signals",
                         negative_signals=[], positive_signals=[{"signal": "occupancy_strong"}])
        detail.pop("pickup_7d_nights")
        text = payload(new=94, occupancy=.9, detail=detail)["text"]
        self.assertIn("€89,00 → €94,00", text)
        self.assertIn("Variazione: +€5,00 / +5,6%", text)
        self.assertIn("Segnali positivi: Occupancy forte", text)
        self.assertNotIn("sconto", text.lower())

    def test_unchanged_price_is_a_valid_maintain_decision(self):
        detail = factors(unsold_risk_pressure=0, urgency_action="hold_no_negative_signals",
                         negative_signals=[], positive_signals=[])
        detail.pop("pickup_7d_nights")
        text = payload(new=89, occupancy=.6, detail=detail, reason="")["text"]
        self.assertIn("€89,00 — MANTIENI", text)
        self.assertIn("Variazione: €0,00 / 0,0%", text)
        self.assertIn("Nessun segnale negativo affidabile", text)

    def test_stored_metadata_is_removed_without_rewriting_engine_reason(self):
        notes = ("plan=plus | requested_mode=approval | event_type=none | event=none | "
                 "conf=0.0 | guardrails=ok | Motivo economico prodotto dal motore.")
        self.assertEqual(telegram_bot._display_reason(notes),
                         "Motivo economico prodotto dal motore.")
        self.assertEqual(telegram_bot._display_reason("Motivo già pulito."),
                         "Motivo già pulito.")

    def test_all_engine_lead_time_bands_are_presented_without_recalculation(self):
        cases = [(74.25, "STANDARD", "74h 15m · STANDARD"),
                 (51.05, "WATCH", "51h 03m · WATCH"),
                 (31 + 20/60, "LAST_MINUTE", "31h 20m · LAST MINUTE"),
                 (19 + 32/60, "URGENT", "19h 32m · URGENT"),
                 (4 + 8/60, "SAME_DAY", "4h 08m · SAME DAY")]
        for hours, band, expected in cases:
            with self.subTest(band=band):
                text = payload(detail=factors(hours_until_checkin=hours,
                                               lead_time_band=band))["text"]
                self.assertIn(expected, text)

    def test_unsold_risk_pressure_is_only_a_simple_ux_label(self):
        cases = [(0, "nessuno"), (.1, "basso"), (.15, "basso"),
                 (.16, "moderato"), (.35, "moderato"), (.36, "elevato")]
        for pressure, expected in cases:
            with self.subTest(pressure=pressure):
                text = payload(detail=factors(unsold_risk_pressure=pressure))["text"]
                self.assertIn(f"Rischio invenduto: {expected}", text)
                self.assertNotIn("unsold_risk_pressure", text)

    def test_all_known_signal_ids_are_translated_without_json(self):
        detail = factors(
            gap_nights=1,
            negative_signals=[{"signal": "occupancy_weak"}, {"signal": "pickup_weak"},
                              {"signal": "confirmed_isolated_gap"}],
            positive_signals=[{"signal": "occupancy_strong"}, {"signal": "pickup_strong"}],
        )
        text = payload(detail=detail)["text"]
        for label in telegram_bot._SIGNAL_LABELS.values():
            self.assertIn(label, text)
        self.assertIn("Gap isolato: confermato (1 notte)", text)
        self.assertNotIn("{'signal'", text)

    def test_signal_conflict_names_the_actual_positive_and_negative_signals(self):
        detail = factors(signal_conflict=True, urgency_action="hold_signal_conflict",
                         negative_signals=[{"signal": "pickup_weak"}],
                         positive_signals=[{"signal": "occupancy_strong"}],
                         rules_confidence="conflicted")
        text = payload(new=94, occupancy=.9, detail=detail)["text"]
        self.assertIn("⚠️ *Segnali contrastanti*", text)
        self.assertIn("Positivi: Occupancy forte", text)
        self.assertIn("Negativi: Pickup debole", text)
        self.assertIn("evitato ulteriore pressione last-minute", text)

    def test_missing_pickup_gap_and_risk_are_omitted_not_invented(self):
        detail = factors()
        for key in ("pickup_7d_nights", "gap_nights", "unsold_risk_pressure"):
            detail.pop(key)
        detail["negative_signals"] = [{"signal": "occupancy_weak"}]
        text = payload(detail=detail)["text"]
        self.assertNotIn("Pickup 7gg", text)
        self.assertNotIn("Gap isolato:", text)
        self.assertNotIn("Rischio invenduto:", text)
        self.assertNotIn("dato non disponibile", text)

    def test_reason_remains_the_primary_explanation(self):
        reason = "Occupancy debole e pickup assente; riduzione entro i guardrail."
        text = payload(reason=reason)["text"]
        self.assertIn(reason, text)
        self.assertGreater(text.index(reason), text.index("💡 *PricePilot*"))

    def test_market_data_is_never_invented_and_real_value_is_explicit(self):
        without_market = payload(market=None)["text"]
        self.assertNotIn("mercato", without_market.lower())
        self.assertNotIn("competitor", without_market.lower())
        with_market = payload(market=101.5)["text"]
        self.assertIn("Mercato osservato nella decisione: €101,50", with_market)

    def test_buttons_show_price_and_bind_both_actions_to_one_decision(self):
        keyboard = payload(new=93.45)["reply_markup"]["inline_keyboard"][0]
        self.assertEqual(keyboard[0], {"text": "✅ Approva €93,45", "callback_data": "approve_42"})
        self.assertEqual(keyboard[1], {"text": "❌ Rifiuta", "callback_data": "reject_42"})

    def test_send_uses_rendered_payload_and_does_not_change_callbacks(self):
        detail = factors(lead_time_band="SAME_DAY")
        with patch.object(telegram_bot, "_api_call", return_value={"ok": True, "result": {"message_id": 8}}) as api, \
             patch("pricepilot.core.database.update_decision_tg_message") as update:
            result = telegram_bot.send_approval_request(
                42, "Luma Pisa", 89, 93.45, .2, None, "", 7,
                "Motivo reale", "2026-10-06", decision_factors=detail,
            )
        self.assertTrue(result["ok"])
        body = api.call_args.args[1]
        buttons = body["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([button["callback_data"] for button in buttons],
                         ["approve_42", "reject_42"])
        update.assert_called_once_with(42, 8)

    def test_input_factors_are_not_mutated(self):
        detail = factors()
        original = deepcopy(detail)
        payload(detail=detail)
        self.assertEqual(detail, original)


if __name__ == "__main__":
    unittest.main()
