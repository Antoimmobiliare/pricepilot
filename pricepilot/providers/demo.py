"""
Default local providers.

These implementations keep the current demo/dev behavior while presenting the
same interface real integrations will use later.
"""
from __future__ import annotations

import hashlib
import os
from datetime import date
from typing import Optional

from pricepilot.core.plans import get_plan, normalize_plan
from pricepilot.providers.contracts import (
    BillingPlanResult,
    ChannelUpdateResult,
    MarketDataResult,
    OccupancyResult,
)


class DemoMarketDataProvider:
    name = "demo_market"

    def analyze(
        self,
        *,
        property_id: int,
        target_date: date,
        event: str = "",
        competitor_count: int = 10,
        account_id: int = 1,
        source: str = "demo",
        persist: bool = True,
    ) -> MarketDataResult:
        from pricepilot.engine.market_analyzer import run_market_analysis

        result = run_market_analysis(
            property_id=property_id,
            target_date=target_date,
            event=event,
            competitor_count=competitor_count,
            persist=persist,
            account_id=account_id,
            source=source,
        )
        return MarketDataResult(
            competitors=result.get("competitors", []),
            market_stats=result.get("market_stats", {}),
            source=source,
            raw=result,
        )


class DemoEventProvider:
    name = "demo_events"

    def event_for_date(self, target_date: date) -> Optional[dict]:
        from pricepilot.data_sources.events import get_event_for_date

        return get_event_for_date(target_date)

    def event_to_string(self, event: Optional[dict]) -> str:
        from pricepilot.data_sources.events import event_to_string

        return event_to_string(event)


class DemoOccupancyProvider:
    name = "demo_occupancy"

    def estimate(
        self,
        *,
        property_id: int,
        target_date: date,
        account_id: int = 1,
    ) -> OccupancyResult:
        seed = f"{account_id}:{property_id}:{target_date.isoformat()}"
        h = int(hashlib.md5(seed.encode()).hexdigest(), 16)
        occupancy = round(0.45 + (h % 1000) / 1000 * 0.45, 2)
        return OccupancyResult(
            occupancy=occupancy,
            source=self.name,
            raw={"seed": seed},
        )


class DefaultChannelManagerProvider:
    name = "channel_manager"

    def update_price(
        self,
        *,
        prop: dict,
        new_price: float,
        target_date: date,
        min_nights: int = 1,
    ) -> ChannelUpdateResult:
        try:
            from pricepilot.integrations.channel_manager import get_channel_manager

            result = get_channel_manager().update_price(
                prop=prop,
                new_price=new_price,
                target_date=target_date,
                min_nights=min_nights,
            )
            raw = getattr(result, "raw", {}) or {}
            return ChannelUpdateResult(
                ok=bool(getattr(result, "ok", False)),
                platform=str(getattr(result, "platform", "") or ""),
                listing_id=str(getattr(result, "listing_id", "") or ""),
                is_real=bool(getattr(result, "ok", False) and not raw.get("stub", True)),
                error=str(getattr(result, "error", "") or ""),
                raw=raw,
            )
        except Exception as exc:
            return ChannelUpdateResult(
                ok=False,
                platform=str(prop.get("platform", "unknown")),
                listing_id=str(prop.get("listing_id", "")),
                is_real=False,
                error=str(exc),
            )


class LocalBillingProvider:
    name = "local_billing"

    def get_account_plan(self, *, account_id: int) -> BillingPlanResult:
        from pricepilot.core.database import get_account

        account = get_account(account_id) or {"plan": "free", "billing_status": "dev"}
        plan = normalize_plan(account.get("plan"))
        plan_info = get_plan(plan)
        return BillingPlanResult(
            plan=plan,
            billing_status=str(account.get("billing_status") or "dev").lower(),
            features=plan_info.get("features", {}),
            raw={"account": account, "plan": plan_info},
        )

    def can_run_manual_cycle(self, *, account: dict, user: Optional[dict] = None) -> bool:
        user = user or {}
        return (
            str(account.get("billing_status", "")).lower() == "dev"
            or str(user.get("role", "")).lower() == "admin"
            or os.environ.get("PRICEPILOT_ALLOW_MANUAL_CYCLE", "").strip() == "1"
        )
