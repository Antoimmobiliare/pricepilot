import os
import unittest
from unittest.mock import patch

from pricepilot.core.operational_mode import operational_mode_enabled, operational_plan
from pricepilot.core.plans import effective_sync_mode
from pricepilot.services.property_service import _enforce_property_limit


class OperationalModeTests(unittest.TestCase):
    def test_explicit_live_flag_enables_single_owner_workflow(self):
        with patch.dict(os.environ, {"PRICEPILOT_ENV": "staging", "PRICEPILOT_OPERATIONAL_MODE": "1"}, clear=True):
            self.assertTrue(operational_mode_enabled())
            self.assertEqual(operational_plan(), "plus")

    def test_unset_or_disabled_flag_preserves_future_saas_ui(self):
        with patch.dict(os.environ, {"PRICEPILOT_ENV": "staging"}, clear=True):
            self.assertFalse(operational_mode_enabled())
        with patch.dict(os.environ, {"PRICEPILOT_ENV": "staging", "PRICEPILOT_OPERATIONAL_MODE": "0"}, clear=True):
            self.assertFalse(operational_mode_enabled())

    def test_plus_rules_never_allow_automatic_write(self):
        self.assertEqual(effective_sync_mode(operational_plan(), "auto"), "approval")

    def test_operational_mode_does_not_depend_on_billing_status(self):
        with patch.dict(os.environ, {"PRICEPILOT_OPERATIONAL_MODE": "1"}, clear=True), \
             patch("pricepilot.services.property_service.get_account", return_value={"plan": "free", "billing_status": "inactive"}), \
             patch("pricepilot.services.property_service.get_properties", return_value=[]):
            _enforce_property_limit({"account_id": 1, "plan": "plus"})
