from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from pricepilot.core.config import CONFIG
from pricepilot.core.database import (
    create_account,
    get_current_price_for_date,
    get_decision_log,
    get_price_calendar,
    init_db,
    save_decision_log,
    update_guardrail_policy,
    upsert_calendar_price,
)
from pricepilot.core.plans import effective_sync_mode, get_plan_limit
import pricepilot.engine.decision_engine as decision_engine
from pricepilot.engine.decision_engine import approve_decision, process_decision
from pricepilot.core.scheduler import run_cloud_pricing_cycle, run_pricing_cycle
from pricepilot.providers.contracts import ChannelUpdateResult, MarketDataResult
from pricepilot.providers.competitors import UnconfiguredCompetitorProvider
from pricepilot.providers.registry import (
    reset_providers,
    set_channel_manager_provider,
    set_event_provider,
    set_market_data_provider,
)
from pricepilot.services.account_service import create_account_owner
from pricepilot.services.property_service import (
    create_property,
    get_property_by_id,
    list_properties,
)
import pricepilot.services.property_service as property_service


TARGET_DATE = date(2026, 5, 18)  # Monday, no weekend boost.


class StaticMarketDataProvider:
    def __init__(self, market_avg: float = 100.0, competitor_count: int = 10):
        self.market_avg = market_avg
        self.competitor_count = competitor_count
        self.name = "test_market"

    def analyze(
        self,
        *,
        property_id: int,
        target_date: date,
        event: str = "",
        competitor_count: int = 10,
        account_id: int = 1,
        source: str = "test",
        persist: bool = True,
    ) -> MarketDataResult:
        count = min(self.competitor_count, competitor_count)
        competitors = [
            {"name": f"Competitor {idx}", "price": self.market_avg}
            for idx in range(count)
        ]
        return MarketDataResult(
            competitors=competitors,
            market_stats={
                "market_avg": self.market_avg,
                "market_min": self.market_avg,
                "market_max": self.market_avg,
                "market_std": 0.0,
                "competitor_count": count,
            },
            source=source,
            raw={"test": True},
        )


class PropertyEventProvider:
    name = "test_property_events"

    def __init__(self):
        self.property_ids = []

    def event_for_property(self, *, prop, target_date, account_id=1):
        self.property_ids.append(int(prop["id"]))
        return {
            "name": f"Evento {prop.get('city')}",
            "event_type": "festival",
            "impact_level": "high",
        }

    def event_for_date(self, target_date):
        return None

    def event_to_string(self, event):
        return str((event or {}).get("event_type") or "none")

    def event_label(self, event):
        return str((event or {}).get("name") or "")


class LiveChannelProvider:
    name = "test_channel"

    def update_price(
        self,
        *,
        prop: dict,
        new_price: float,
        target_date: date,
        min_nights: int = 1,
    ) -> ChannelUpdateResult:
        return ChannelUpdateResult(
            ok=True,
            platform="test_channel",
            listing_id=str(prop.get("listing_id") or "listing-test"),
            is_real=True,
            raw={"test": True},
        )


class StubChannelProvider:
    name = "test_channel_stub"

    def update_price(
        self,
        *,
        prop: dict,
        new_price: float,
        target_date: date,
        min_nights: int = 1,
    ) -> ChannelUpdateResult:
        return ChannelUpdateResult(
            ok=True,
            platform="test_channel",
            listing_id=str(prop.get("listing_id") or "listing-test"),
            is_real=False,
            raw={"stub": True},
        )


class PricePilotSaaSTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = CONFIG["db_path"]
        self._old_database_backend = os.environ.get("PRICEPILOT_DATABASE_BACKEND")
        # These unit tests must never write to the configured cloud tenant.
        os.environ["PRICEPILOT_DATABASE_BACKEND"] = "sqlite"
        self._cloud_sync_patches = [
            patch.object(property_service, "refresh_properties_from_supabase", return_value=[]),
            patch.object(property_service, "backfill_account_properties_to_supabase", return_value=None),
            patch.object(property_service, "sync_property_and_pricing_to_supabase", return_value=None),
        ]
        for cloud_patch in self._cloud_sync_patches:
            cloud_patch.start()
        CONFIG["db_path"] = str(Path(self._tmp.name) / "pricepilot_test.db")
        init_db()
        reset_providers()
        set_market_data_provider(StaticMarketDataProvider())
        set_channel_manager_provider(LiveChannelProvider())
        self._old_notify_price_change = decision_engine.notify_price_change
        decision_engine.notify_price_change = lambda *args, **kwargs: None

    def tearDown(self):
        decision_engine.notify_price_change = self._old_notify_price_change
        reset_providers()
        for cloud_patch in reversed(self._cloud_sync_patches):
            cloud_patch.stop()
        CONFIG["db_path"] = self._old_db_path
        if self._old_database_backend is None:
            os.environ.pop("PRICEPILOT_DATABASE_BACKEND", None)
        else:
            os.environ["PRICEPILOT_DATABASE_BACKEND"] = self._old_database_backend
        self._tmp.cleanup()

    def _account(self, plan: str, name: str | None = None) -> dict:
        return create_account(name or f"Account {plan}", plan=plan, billing_status="active")

    def _property(self, account: dict, name: str = "Test Property") -> dict:
        plan = account["plan"]
        return create_property({
            "account_id": account["id"],
            "name": name,
            "platform": "airbnb",
            "listing_url": "",
            "listing_id": f"listing-{account['id']}-{name}",
            "city": "Lucca",
            "min_price": 50.0,
            "max_price": 150.0,
            "plan": plan,
            "sync_mode": effective_sync_mode(plan, "auto"),
        })

    def test_signup_selected_plan_is_saved_on_account(self):
        result = create_account_owner(
            email="plus-owner@example.test",
            password_hash="test-hash",
            account_name="Plus Host",
            plan="plus",
        )

        self.assertEqual(result["account"]["plan"], "plus")
        self.assertEqual(result["user"]["account_id"], result["account"]["id"])

    def test_cloud_scheduler_runs_once_for_each_account_with_properties(self):
        account_a = self._account("free", "Cloud Host A")
        account_b = self._account("plus", "Cloud Host B")
        self._account("free", "No Property Host")
        self._property(account_a, "Cloud Apt A")
        self._property(account_b, "Cloud Apt B")

        with patch("pricepilot.core.scheduler.run_pricing_cycle", return_value={"run": {}, "results": [], "errors": []}) as run_cycle:
            result = run_cloud_pricing_cycle(source="unit_test_cloud")

        processed = {call.kwargs["account_id"] for call in run_cycle.call_args_list}
        self.assertEqual(processed, {account_a["id"], account_b["id"]})
        self.assertEqual(result["accounts_processed"], 2)
        self.assertEqual(result["accounts_failed"], 0)

    def test_account_isolation_for_properties_decisions_and_approval(self):
        account_a = self._account("free", "Host A")
        account_b = self._account("free", "Host B")
        prop_a = self._property(account_a, "Apt A")
        prop_b = self._property(account_b, "Apt B")

        self.assertEqual([p["id"] for p in list_properties(account_a["id"])], [prop_a["id"]])
        self.assertEqual([p["id"] for p in list_properties(account_b["id"])], [prop_b["id"]])
        self.assertIsNone(get_property_by_id(prop_a["id"], account_id=account_b["id"]))

        log_id = save_decision_log({
            "account_id": account_a["id"],
            "property_id": prop_a["id"],
            "old_price": 100.0,
            "new_price": 110.0,
            "market_avg": 105.0,
            "occupancy": 0.65,
            "decision": "PENDING_APPROVAL",
            "mode": "approval",
            "applied": 0,
            "date": TARGET_DATE.isoformat(),
        })
        self.assertEqual(len(get_decision_log(account_id=account_a["id"])), 1)
        self.assertEqual(len(get_decision_log(account_id=account_b["id"])), 0)

        forbidden = approve_decision(log_id, account_id=account_b["id"])
        self.assertFalse(forbidden["approved"])
        self.assertEqual(forbidden["status"], "forbidden")

    def test_plan_limits_and_allowed_modes(self):
        self.assertEqual(get_plan_limit("free", "max_properties"), 1)
        self.assertEqual(get_plan_limit("plus", "max_properties"), 5)
        self.assertEqual(get_plan_limit("pro", "max_properties"), 25)
        self.assertEqual(effective_sync_mode("free", "auto"), "advisory")
        self.assertEqual(effective_sync_mode("plus", "auto"), "approval")
        self.assertEqual(effective_sync_mode("pro", "approval"), "approval")
        self.assertEqual(effective_sync_mode("pro", "auto"), "approval")

        free_account = self._account("free", "Free Host")
        self._property(free_account, "Free Apt 1")
        with self.assertRaises(ValueError):
            self._property(free_account, "Free Apt 2")

        plus_account = self._account("plus", "Plus Host")
        self._property(plus_account, "Plus Apt 1")
        self._property(plus_account, "Plus Apt 2")
        self.assertEqual(len(list_properties(plus_account["id"])), 2)

    def test_free_decision_is_advisory_only(self):
        account = self._account("free", "Free Decision")
        prop = self._property(account)

        result = process_decision(
            property_id=prop["id"],
            occupancy=0.65,
            target_date=TARGET_DATE,
            competitor_count=10,
            data_source="test",
            occupancy_source="test",
        )

        self.assertEqual(result["mode"], "advisory")
        self.assertFalse(result["applied"])
        self.assertTrue(result["decision"].startswith("ADVISORY"))
        self.assertEqual(result["calendar_status"], "recommended")

    def test_plus_decision_waits_for_approval(self):
        account = self._account("plus", "Plus Decision")
        prop = self._property(account)

        result = process_decision(
            property_id=prop["id"],
            occupancy=0.65,
            target_date=TARGET_DATE,
            competitor_count=10,
            data_source="test",
            occupancy_source="test",
        )

        self.assertEqual(result["mode"], "approval")
        self.assertFalse(result["applied"])
        self.assertTrue(result["decision"].startswith("PENDING_APPROVAL"))
        self.assertEqual(result["calendar_status"], "pending_approval")

    def test_pro_requires_approval_before_channel_write(self):
        account = self._account("pro", "Pro Decision")
        prop = self._property(account)

        ok_result = process_decision(
            property_id=prop["id"],
            occupancy=0.65,
            target_date=TARGET_DATE,
            competitor_count=10,
            data_source="test",
            occupancy_source="test",
        )

        self.assertEqual(ok_result["requested_mode"], "approval")
        self.assertEqual(ok_result["mode"], "approval")
        self.assertFalse(ok_result["applied"])
        self.assertTrue(ok_result["decision"].startswith("PENDING_APPROVAL"))
        self.assertEqual(ok_result["calendar_status"], "pending_approval")

        guarded_account = self._account("pro", "Guarded Pro")
        guarded_prop = self._property(guarded_account)
        update_guardrail_policy(
            account_id=guarded_account["id"],
            data={"auto_enabled": 0},
        )
        guarded_result = process_decision(
            property_id=guarded_prop["id"],
            occupancy=0.65,
            target_date=TARGET_DATE,
            competitor_count=10,
            data_source="test",
            occupancy_source="test",
        )

        self.assertEqual(guarded_result["requested_mode"], "approval")
        self.assertEqual(guarded_result["mode"], "approval")
        self.assertFalse(guarded_result["applied"])

    def test_pro_remains_approval_without_market_provider(self):
        set_market_data_provider(UnconfiguredCompetitorProvider())
        account = self._account("pro", "Pro Without Competitors")
        prop = self._property(account)

        result = process_decision(
            property_id=prop["id"],
            occupancy=0.65,
            target_date=TARGET_DATE,
            competitor_count=10,
            data_source="unconfigured",
            occupancy_source="test",
        )

        self.assertEqual(result["requested_mode"], "approval")
        self.assertEqual(result["mode"], "approval")
        self.assertFalse(result["applied"])

    def test_scheduler_resolves_events_for_the_specific_property(self):
        event_provider = PropertyEventProvider()
        set_event_provider(event_provider)
        account = self._account("free", "Event Location Host")
        prop = self._property(account, "Lucca Event Apartment")

        result = run_pricing_cycle(
            account_id=account["id"],
            target_date=TARGET_DATE,
            source="unit_test_event_location",
        )

        self.assertEqual(event_provider.property_ids, [prop["id"]])
        self.assertEqual(result["results"][0]["event"], "Evento Lucca")
        self.assertEqual(result["results"][0]["event_type"], "festival")

    def test_locked_calendar_price_is_not_changed_by_pricing_cycle(self):
        account = self._account("plus", "Calendar Host")
        prop = self._property(account)
        upsert_calendar_price({
            "account_id": account["id"],
            "property_id": prop["id"],
            "date": TARGET_DATE.isoformat(),
            "current_price": 250.0,
            "current_price_source": "manual",
            "status": "locked",
            "notes": "Lucca Comics",
        })

        current_price, source = get_current_price_for_date(prop, TARGET_DATE.isoformat())
        self.assertEqual(current_price, 250.0)
        self.assertEqual(source, "manual_lock")

        result = process_decision(
            property_id=prop["id"],
            occupancy=0.90,
            target_date=TARGET_DATE,
            competitor_count=10,
            data_source="test",
            occupancy_source="test",
        )

        self.assertEqual(result["recommended_price"], 250.0)
        self.assertEqual(result["guardrail_status"], "locked")
        self.assertEqual(result["calendar_status"], "locked")
        calendar = get_price_calendar(account_id=account["id"], property_id=prop["id"])
        self.assertEqual(calendar[0]["current_price"], 250.0)
        self.assertEqual(calendar[0]["status"], "locked")

    def test_telegram_approval_without_channel_manager_stays_pending_manual_sync(self):
        set_channel_manager_provider(StubChannelProvider())
        account = self._account("plus", "Telegram Approval")
        prop = self._property(account)
        result = process_decision(
            property_id=prop["id"],
            occupancy=0.65,
            target_date=TARGET_DATE,
            competitor_count=10,
            data_source="test",
            occupancy_source="test",
        )

        approval = approve_decision(result["log_id"], account_id=account["id"])

        self.assertTrue(approval["approved"])
        self.assertFalse(approval["applied"])
        self.assertEqual(approval["status"], "approved_pending_manual_sync")

        calendar = get_price_calendar(account_id=account["id"], property_id=prop["id"])
        self.assertEqual(calendar[0]["status"], "approved_pending_manual_sync")
        self.assertIsNone(calendar[0]["applied_price"])

    def test_telegram_approval_with_real_channel_manager_applies_price(self):
        account = self._account("plus", "Telegram Live Approval")
        prop = self._property(account)
        result = process_decision(
            property_id=prop["id"],
            occupancy=0.65,
            target_date=TARGET_DATE,
            competitor_count=10,
            data_source="test",
            occupancy_source="test",
        )

        approval = approve_decision(result["log_id"], account_id=account["id"])

        self.assertTrue(approval["approved"])
        self.assertTrue(approval["applied"])
        self.assertEqual(approval["status"], "applied")
        self.assertTrue(approval["channel_manager"]["is_real"])

        decision = get_decision_log(account_id=account["id"])[0]
        self.assertEqual(decision["applied"], 1)
        self.assertIn("[APPROVED_SYNCED]", decision["decision"])

        calendar = get_price_calendar(account_id=account["id"], property_id=prop["id"])
        self.assertEqual(calendar[0]["status"], "applied")
        self.assertEqual(calendar[0]["applied_price"], approval["channel_manager"].get("new_price", result["recommended_price"]))


if __name__ == "__main__":
    unittest.main()
