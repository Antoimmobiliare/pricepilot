"""Full Streamlit entry point, not just isolated view components."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from pricepilot.core import config
from pricepilot.providers.registry import reset_providers

APP = Path(__file__).resolve().parents[1] / 'pricepilot' / 'dashboard' / 'app.py'


class DashboardStartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db = config.CONFIG['db_path']
        config.CONFIG['db_path'] = str(Path(self.tmp.name) / 'dashboard.db')
        self.addCleanup(lambda: config.CONFIG.update(db_path=self.old_db))
        self.config_patch = patch.object(config, 'CONFIG_FILE', Path(self.tmp.name) / 'config.json')
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.env = patch.dict(os.environ, {'PRICEPILOT_ENV': 'development',
            'PRICEPILOT_DATA_PROVIDER': 'unconfigured', 'PRICEPILOT_AUTH_MODE': 'local'})
        self.env.start()
        self.addCleanup(self.env.stop)
        reset_providers()
        self.addCleanup(reset_providers)

    def test_public_entry_point_loads_without_credentials(self):
        app = AppTest.from_file(str(APP)).run(timeout=30)
        self.assertEqual(len(app.exception), 0, str(app.exception))
        self.assertGreater(len(app.button), 0)

    def test_full_dashboard_and_navigation_without_external_connections(self):
        # Explicit local-development bypass only for testing the authenticated UI.
        with patch.dict(os.environ, {'PRICEPILOT_AUTH_MODE': 'disabled'}):
            app = AppTest.from_file(str(APP)).run(timeout=30)
            self.assertEqual(len(app.exception), 0, str(app.exception))
            navigation = app.radio(key='pp_main_section_label')
            for label in list(navigation.options):
                with self.subTest(section=label):
                    app.radio(key='pp_main_section_label').set_value(label).run(timeout=30)
                    self.assertEqual(len(app.exception), 0, str(app.exception))

    def test_empty_account_has_direct_property_onboarding_cta(self):
        with patch.dict(os.environ, {'PRICEPILOT_AUTH_MODE': 'disabled'}):
            app = AppTest.from_file(str(APP)).run(timeout=30)
            cta = next(
                button for button in app.button
                if button.label == 'Aggiungi il primo appartamento'
            )
            cta.click().run(timeout=30)
            self.assertEqual(app.radio(key='pp_main_section_label').value, '🏡 Proprietà')
            self.assertEqual(len(app.exception), 0, str(app.exception))
