"""Approval queue classification must follow the authoritative predicate."""
import unittest
from unittest.mock import patch

from pricepilot.dashboard import app


class DashboardDecisionQueueTests(unittest.TestCase):
    def test_unchanged_is_never_classified_as_pending(self):
        status = app._decision_flow_status({
            "id": 280,
            "old_price": 89,
            "new_price": 89,
            "decision": "UNCHANGED: tariffa gia allineata",
            "mode": "approval",
            "applied": 0,
        })
        self.assertEqual(status[0], "suggested")
        self.assertNotEqual(status[0], "pending")

    def test_terminal_sync_failure_is_not_pending(self):
        status = app._decision_flow_status({
            "id": 215,
            "old_price": 89,
            "new_price": 93.45,
            "decision": "PENDING_APPROVAL: 89.00->93.45 (+5.0%) [APPROVED_SYNC_FAILED]",
            "mode": "approval",
            "applied": 0,
        })
        self.assertNotEqual(status[0], "pending")

    def test_dashboard_uses_shared_authoritative_pending_ids(self):
        with patch.object(app, "_cached_pending_approvals", return_value=[{"id": 214}]):
            self.assertEqual(app._authoritative_pending_ids(4), {214})


if __name__ == "__main__":
    unittest.main()
