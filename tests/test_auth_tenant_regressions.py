"""Regressioni auth/tenant isolate: nessun DB reale e nessuna rete."""
from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pricepilot.dashboard import auth
from pricepilot.services import supabase_primary


class DashboardAuthTenantRegressionTests(unittest.TestCase):
    @staticmethod
    def _streamlit_stub(state: dict | None = None):
        return SimpleNamespace(
            session_state={} if state is None else state,
            error=Mock(),
            info=Mock(),
            success=Mock(),
            rerun=Mock(),
        )

    def test_current_account_id_never_falls_back_to_first_tenant(self):
        for user in (None, {}, {"id": 9}, {"id": 9, "account_id": 0}, {"id": 9, "account_id": "bad"}):
            state = {} if user is None else {auth._KEY_USER: user}
            with self.subTest(user=user), patch.object(auth, "st", self._streamlit_stub(state)):
                with self.assertRaises((RuntimeError, ValueError)):
                    auth.get_current_account_id()

    def test_local_session_rejects_user_without_valid_account(self):
        for user in ({"id": 9, "email": "owner@example.test"},
                     {"id": 9, "email": "owner@example.test", "account_id": 0}):
            st = self._streamlit_stub()
            with self.subTest(user=user), patch.object(auth, "st", st):
                with self.assertRaises((RuntimeError, ValueError)):
                    auth._set_local_session(user)
                self.assertNotIn(auth._KEY_USER, st.session_state)

    def test_signup_consent_never_writes_to_default_account(self):
        incomplete_user = {"id": 9, "email": "owner@example.test"}
        with patch.object(auth, "get_latest_user_consent") as latest, \
             patch.object(auth, "record_user_consent") as record:
            with self.assertRaises((RuntimeError, ValueError)):
                auth._record_signup_consent(
                    incomplete_user,
                    terms_accepted=True,
                    marketing_accepted=False,
                )

        latest.assert_not_called()
        record.assert_not_called()

    def test_pending_supabase_email_does_not_create_tenant_before_confirmation(self):
        response = SimpleNamespace(
            user=SimpleNamespace(id="supabase-user-1", email="new@example.test"),
            session=None,
        )
        client = SimpleNamespace(auth=SimpleNamespace(sign_up=Mock(return_value=response)))
        st = self._streamlit_stub()

        with patch.object(auth, "st", st), \
             patch.object(auth, "_ensure_external_user") as ensure_user, \
             patch.object(auth, "_record_signup_consent") as record_consent:
            auth._do_signup(
                client,
                "new@example.test",
                "secret12",
                "Nuovo host",
                "plus",
                terms_accepted=True,
                marketing_accepted=False,
            )

        ensure_user.assert_not_called()
        record_consent.assert_not_called()
        self.assertEqual(st.session_state.get(auth._KEY_PUBLIC_VIEW), "login")
        self.assertEqual(st.session_state.get("auth_login_email"), "new@example.test")


class SupabaseMembershipRegressionTests(unittest.TestCase):
    PROFILE = {
        "id": "supabase-owner-1",
        "local_id": 91,
        "email": "owner@example.test",
        "full_name": "Owner",
    }

    @staticmethod
    def _select_with_account_owner(owner_user_id: str):
        def select(table, **kwargs):
            if table == "account_members":
                return [{
                    "account_id": 7,
                    "user_id": "supabase-owner-1",
                    "role": "owner",
                    "created_at": "2026-01-01T00:00:00Z",
                }]
            if table == "accounts":
                return [{"id": 7, "owner_user_id": owner_user_id}]
            raise AssertionError(f"Query inattesa: {table} {kwargs}")

        return select

    def test_owner_membership_is_rejected_when_account_owner_differs(self):
        with patch.object(
            supabase_primary,
            "_select",
            side_effect=self._select_with_account_owner("different-supabase-user"),
        ):
            user = supabase_primary._cloud_user_from_profile(self.PROFILE)

        self.assertIsNone(user)

    def test_owner_membership_is_accepted_when_account_owner_matches(self):
        with patch.object(
            supabase_primary,
            "_select",
            side_effect=self._select_with_account_owner("supabase-owner-1"),
        ):
            user = supabase_primary._cloud_user_from_profile(self.PROFILE)

        self.assertIsNotNone(user)
        self.assertEqual(user["account_id"], 7)
        self.assertEqual(user["external_user_id"], "supabase-owner-1")


if __name__ == "__main__":
    unittest.main()
