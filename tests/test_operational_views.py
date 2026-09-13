"""Render operational screens without credentials, network or persistent data."""
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from pricepilot.dashboard import operational


class OperationalViewTests(unittest.TestCase):
    def render(self, section, rows=None):
        with ExitStack() as stack:
            stack.enter_context(patch.object(operational, 'get_occupancy_provider',
                return_value=SimpleNamespace(name='unconfigured')))
            for name in ('get_operation_runs', 'get_decision_log', 'get_price_calendar'):
                stack.enter_context(patch.object(operational, name, return_value=rows or []))
            stack.enter_context(patch.object(operational, 'get_reservation_metrics',
                return_value={'complete': False}))
            stack.enter_context(patch.object(operational, 'property_readiness', return_value={
                'configured': False, 'analysis_ready': False,
                'approval_ready': False, 'write_ready': False, 'checks': [],
            }))
            app = AppTest.from_string(
                "from pricepilot.dashboard.operational import render\n"
                f"render(1, {section!r}, property_id=2)\n").run(timeout=15)
        self.assertEqual(len(app.exception), 0, str(app.exception))
        self.assertEqual(len(app.warning), 1)
        return app

    def test_all_unconfigured_views_render_without_fabricated_metrics(self):
        for section in ('home', 'calendar', 'analytics', 'pricing'):
            with self.subTest(section=section):
                app = self.render(section)
                self.assertEqual(len(app.metric), 4 if section == 'home' else 0)
                self.assertEqual(len(app.dataframe), 0)
                if section == 'pricing':
                    self.assertTrue(app.button[0].disabled)

    def test_recorded_rates_remain_separate_from_recommendation(self):
        app = self.render('calendar', [{'date': '2026-10-01', 'current_price': 100,
                                       'recommended_price': 110, 'applied_price': None}])
        frame = app.dataframe[0].value
        self.assertEqual(frame['Prezzo letto'][0], 100)
        self.assertEqual(frame['Prezzo proposto'][0], 110)
        self.assertTrue(frame['Prezzo inviato'].isna()[0])

    def test_calendar_pricing_view_does_not_require_competitor_provider(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(operational, 'get_occupancy_provider',
                return_value=SimpleNamespace(name='observed_inventory')))
            stack.enter_context(patch.object(operational, 'property_readiness',
                return_value={'analysis_ready': True}))
            for name in ('get_operation_runs', 'get_decision_log'):
                stack.enter_context(patch.object(operational, name, return_value=[]))
            app = AppTest.from_string('from pricepilot.dashboard.operational import render\nrender(1, "pricing", 2)').run(timeout=15)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.warning), 0)
        self.assertFalse(app.button[0].disabled)

    def test_real_metrics_show_unknown_money_without_inventing_it(self):
        metrics = {'complete': True, 'occupancy': .5, 'booked_nights': 5,
                   'available_nights': 10, 'adr': None, 'revpar': None,
                   'pickup_7d_nights': None}
        with ExitStack() as stack:
            stack.enter_context(patch.object(operational, 'get_occupancy_provider',
                return_value=SimpleNamespace(name='observed_inventory')))
            stack.enter_context(patch.object(operational, 'get_reservation_metrics', return_value=metrics))
            stack.enter_context(patch.object(operational, 'get_decision_log', return_value=[]))
            app = AppTest.from_string(
                'from pricepilot.dashboard.operational import render\nrender(1, "analytics", 2)'
            ).run(timeout=15)
        self.assertEqual(len(app.exception), 0, str(app.exception))
        values = [metric.value for metric in app.metric]
        self.assertIn('50.0%', values)
        self.assertEqual(values.count('Non disponibile'), 3)
