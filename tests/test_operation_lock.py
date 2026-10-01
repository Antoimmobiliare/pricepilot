from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pricepilot.core import config
from pricepilot.core.data_quality import DataUnavailable
from pricepilot.core.operation_lock import pricing_date_lease


class PricingDateLeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        old = config.CONFIG['db_path']; config.CONFIG['db_path'] = str(Path(self.tmp.name)/'locks.db')
        self.addCleanup(lambda: config.CONFIG.update(db_path=old))

    def test_same_date_cannot_run_twice_and_release_allows_next(self):
        day = date.today().isoformat()
        with pricing_date_lease(1, 2, day):
            with self.assertRaises(DataUnavailable):
                with pricing_date_lease(1, 2, day): pass
        with pricing_date_lease(1, 2, day, ttl_seconds=30):
            pass

    def test_scope_keys_are_independent(self):
        day = date.today().isoformat()
        with pricing_date_lease(1, 2, day), pricing_date_lease(2, 2, day), pricing_date_lease(1, 3, day):
            pass

    def test_cloud_rpc_acquire_release_and_contention(self):
        responses = [True, False, True]
        client = Mock()
        def rpc(name, payload):
            result = Mock()
            result.execute.return_value = Mock(data=(responses.pop(0) if name.startswith("acquire") else True))
            return result
        client.rpc.side_effect = rpc
        day = date.today().isoformat()
        with patch("pricepilot.core.operation_lock.is_supabase_primary", return_value=True), \
             patch("pricepilot.core.operation_lock.get_supabase_admin_client", return_value=client):
            with pricing_date_lease(1, 2, day):
                with self.assertRaises(DataUnavailable):
                    with pricing_date_lease(1, 2, day):
                        pass
            with pricing_date_lease(1, 2, day):
                pass
        calls = [call.args[0] for call in client.rpc.call_args_list]
        self.assertEqual(calls, [
            "acquire_pricepilot_pricing_lock",
            "acquire_pricepilot_pricing_lock",
            "release_pricepilot_pricing_lock",
            "acquire_pricepilot_pricing_lock",
            "release_pricepilot_pricing_lock",
        ])

    def test_cloud_rpc_error_fails_closed_without_leaking_body(self):
        client = Mock()
        client.rpc.side_effect = RuntimeError("token=secret body=private")
        day = date.today().isoformat()
        with patch("pricepilot.core.operation_lock.is_supabase_primary", return_value=True), \
             patch("pricepilot.core.operation_lock.get_supabase_admin_client", return_value=client):
            with self.assertRaisesRegex(Exception, "Lock cloud RPC rifiutata") as ctx:
                with pricing_date_lease(1, 2, day):
                    pass
        self.assertNotIn("secret", str(ctx.exception))

    def test_expired_cycle_deadline_fails_before_cloud_rpc(self):
        client = Mock()
        day = date.today().isoformat()
        with patch("pricepilot.core.operation_lock.is_supabase_primary", return_value=True), \
             patch("pricepilot.core.operation_lock.get_supabase_admin_client", return_value=client):
            with self.assertRaisesRegex(Exception, "entro il limite del ciclo"):
                with pricing_date_lease(1, 2, day, deadline=0):
                    pass
        client.rpc.assert_not_called()
