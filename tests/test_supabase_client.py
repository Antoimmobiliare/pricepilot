import sys
import types
import unittest
from unittest.mock import patch

from pricepilot.core.supabase_client import _setting


class SupabaseSettingTests(unittest.TestCase):
    def test_streamlit_top_level_secret_is_read_without_logging(self):
        fake = types.SimpleNamespace(secrets={"SUPABASE_SERVICE_ROLE_KEY": "  secret-value  "})
        with patch.dict(sys.modules, {"streamlit": fake}), patch.dict("os.environ", {}, clear=False):
            with patch("pricepilot.core.supabase_client.os.environ.get", return_value=""):
                self.assertEqual(_setting("SUPABASE_SERVICE_ROLE_KEY"), "secret-value")

    def test_streamlit_default_section_is_supported(self):
        fake = types.SimpleNamespace(secrets={"default": {"SUPABASE_URL": "https://example.test"}})
        with patch.dict(sys.modules, {"streamlit": fake}), patch("pricepilot.core.supabase_client.os.environ.get", return_value=""):
            self.assertEqual(_setting("SUPABASE_URL"), "https://example.test")


if __name__ == "__main__":
    unittest.main()
