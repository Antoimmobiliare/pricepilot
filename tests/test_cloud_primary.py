"""Contratti locali per il cutover Supabase, senza credenziali reali."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch


class CloudBackendTests(unittest.TestCase):
    def test_unknown_backend_is_rejected(self):
        from pricepilot.core import data_backend

        with patch.dict(os.environ, {"PRICEPILOT_DATABASE_BACKEND": "unknown"}, clear=False):
            with self.assertRaises(data_backend.CloudDatabaseUnavailable):
                data_backend.database_backend()

    def test_dashboard_scopes_cloud_query_to_current_account(self):
        from pricepilot.services import supabase_primary

        with patch.object(supabase_primary, "has_supabase_auth_session", return_value=False), \
             patch.object(supabase_primary, "server_runtime", return_value=False), \
             patch.object(supabase_primary, "_dashboard_account_id", return_value=42):
            filters = supabase_primary._scoped_filters("properties")

        self.assertEqual(filters, {"account_id": 42})

    def test_dashboard_rejects_unscoped_cloud_query_without_account(self):
        from pricepilot.services import supabase_primary

        with patch.object(supabase_primary, "has_supabase_auth_session", return_value=False), \
             patch.object(supabase_primary, "server_runtime", return_value=False), \
             patch.object(supabase_primary, "_dashboard_account_id", return_value=None):
            with self.assertRaises(supabase_primary.CloudDatabaseUnavailable):
                supabase_primary._scoped_filters("properties")

    def test_cutover_schema_contains_cloud_primary_contract(self):
        root = Path(__file__).resolve().parents[1]
        sql = (root / "supabase" / "cloud_primary_cutover.sql").read_text(encoding="utf-8").lower()

        self.assertIn("create table if not exists public.app_sessions", sql)
        self.assertIn("'properties'", sql)
        self.assertIn("create table if not exists public.price_updates", sql)
        self.assertIn("cloud_primary_cutover_ready", sql)
        self.assertNotIn("drop table", sql)


if __name__ == "__main__":
    unittest.main()
