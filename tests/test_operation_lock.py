from datetime import date
from pathlib import Path
import tempfile
import unittest

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
