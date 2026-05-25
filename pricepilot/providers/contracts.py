"""
Stable integration contracts for PricePilot providers.

The pricing engine should depend on these interfaces, not on concrete APIs.
When we connect real services later, we swap provider implementations without
rewriting the engine or scheduler.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Protocol


@dataclass(frozen=True)
class MarketDataResult:
    competitors: list[dict]
    market_stats: dict
    source: str = "demo"
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OccupancyResult:
    occupancy: float
    source: str = "demo"
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelUpdateResult:
    ok: bool
    platform: str = ""
    listing_id: str = ""
    is_real: bool = False
    error: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BillingPlanResult:
    plan: str
    billing_status: str = "dev"
    features: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


class MarketDataProvider(Protocol):
    """Reads competitor/market signals for a property and date."""

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
        ...


class EventProvider(Protocol):
    """Reads relevant local events for a target date."""

    def event_for_date(self, target_date: date) -> Optional[dict]:
        ...

    def event_to_string(self, event: Optional[dict]) -> str:
        ...


class OccupancyProvider(Protocol):
    """Reads or estimates occupancy for a property and date."""

    def estimate(
        self,
        *,
        property_id: int,
        target_date: date,
        account_id: int = 1,
    ) -> OccupancyResult:
        ...


class ChannelManagerProvider(Protocol):
    """Applies price updates to external channels."""

    def update_price(
        self,
        *,
        prop: dict,
        new_price: float,
        target_date: date,
        min_nights: int = 1,
    ) -> ChannelUpdateResult:
        ...


class BillingProvider(Protocol):
    """Resolves account entitlements and billing-related permissions."""

    def get_account_plan(self, *, account_id: int) -> BillingPlanResult:
        ...

    def can_run_manual_cycle(self, *, account: dict, user: Optional[dict] = None) -> bool:
        ...
