"""
Provider registry for PricePilot.

Production integrations should be registered here at app startup. The rest of
the codebase reads providers through these getters.
"""
from __future__ import annotations

import os

from pricepilot.providers.contracts import (
    BillingProvider,
    ChannelManagerProvider,
    EventProvider,
    MarketDataProvider,
    OccupancyProvider,
)
from pricepilot.providers.demo import (
    DefaultChannelManagerProvider,
    DemoEventProvider,
    DemoMarketDataProvider,
    DemoOccupancyProvider,
    LocalBillingProvider,
)
from pricepilot.providers.competitors import UnconfiguredCompetitorProvider, UnconfiguredOccupancyProvider
from pricepilot.core.data_quality import demo_enabled, live_mode
from pricepilot.providers.free_events import FreeEventProvider
from pricepilot.providers.manual import (
    ManualEventProvider,
    ManualMarketDataProvider,
    ManualOccupancyProvider,
)
from pricepilot.providers.stripe_billing import StripeBillingProvider


def _data_provider_mode() -> str:
    return os.environ.get("PRICEPILOT_DATA_PROVIDER", "unconfigured").strip().lower()


def _provider_mode(name: str) -> str:
    return os.environ.get(name, "").strip().lower()


def _is_cloud_runtime() -> bool:
    return live_mode()


def _default_market_data_provider() -> MarketDataProvider:
    mode = _provider_mode("PRICEPILOT_MARKET_PROVIDER")
    if mode == "observed_quotes":
        from pricepilot.providers.observations import ObservedQuotesProvider
        return ObservedQuotesProvider()
    if mode in {"manual", "csv", "manual_csv"}:
        return ManualMarketDataProvider()
    if mode == "demo" and demo_enabled():
        return DemoMarketDataProvider()
    if mode in {"none", "disabled", "unconfigured"}:
        return UnconfiguredCompetitorProvider()
    if _data_provider_mode() in {"manual", "csv", "manual_csv"}:
        return ManualMarketDataProvider()
    if _is_cloud_runtime():
        return UnconfiguredCompetitorProvider()
    return DemoMarketDataProvider() if demo_enabled() else UnconfiguredCompetitorProvider()


def _default_event_provider() -> EventProvider:
    mode = _provider_mode("PRICEPILOT_EVENT_PROVIDER")
    if mode in {"manual", "csv", "manual_csv"}:
        return ManualEventProvider()
    if mode == "demo" and demo_enabled():
        return DemoEventProvider()
    if _data_provider_mode() in {"manual", "csv", "manual_csv"}:
        return ManualEventProvider()
    return FreeEventProvider()


def _default_occupancy_provider() -> OccupancyProvider:
    if _provider_mode("PRICEPILOT_OCCUPANCY_PROVIDER") == "observed_inventory":
        from pricepilot.providers.observations import ObservedInventoryProvider
        return ObservedInventoryProvider()
    if _data_provider_mode() in {"manual", "csv", "manual_csv"}:
        return ManualOccupancyProvider()
    return DemoOccupancyProvider() if demo_enabled() else UnconfiguredOccupancyProvider()


_market_data_provider: MarketDataProvider = _default_market_data_provider()
_event_provider: EventProvider = _default_event_provider()
_occupancy_provider: OccupancyProvider = _default_occupancy_provider()
def _default_channel_manager_provider():
    if _provider_mode("PRICEPILOT_CHANNEL_PROVIDER") == "beds24":
        from pricepilot.integrations.beds24 import Beds24ChannelProvider
        return Beds24ChannelProvider()
    return DefaultChannelManagerProvider()


_channel_manager_provider: ChannelManagerProvider = _default_channel_manager_provider()

def _default_billing_provider() -> BillingProvider:
    stripe_env_present = any(
        os.environ.get(key, "").strip()
        for key in ("STRIPE_SECRET_KEY", "STRIPE_PRICE_PLUS", "STRIPE_PRICE_PRO")
    )
    if stripe_env_present:
        return StripeBillingProvider()
    return LocalBillingProvider()


_billing_provider: BillingProvider = _default_billing_provider()


def get_market_data_provider() -> MarketDataProvider:
    return _market_data_provider


def set_market_data_provider(provider: MarketDataProvider) -> None:
    global _market_data_provider
    _market_data_provider = provider


def get_event_provider() -> EventProvider:
    return _event_provider


def set_event_provider(provider: EventProvider) -> None:
    global _event_provider
    _event_provider = provider


def get_occupancy_provider() -> OccupancyProvider:
    return _occupancy_provider


def set_occupancy_provider(provider: OccupancyProvider) -> None:
    global _occupancy_provider
    _occupancy_provider = provider


def get_channel_manager_provider() -> ChannelManagerProvider:
    return _channel_manager_provider


def set_channel_manager_provider(provider: ChannelManagerProvider) -> None:
    global _channel_manager_provider
    _channel_manager_provider = provider


def get_billing_provider() -> BillingProvider:
    return _billing_provider


def set_billing_provider(provider: BillingProvider) -> None:
    global _billing_provider
    _billing_provider = provider


def reset_providers() -> None:
    global _market_data_provider
    global _event_provider
    global _occupancy_provider
    global _channel_manager_provider
    global _billing_provider

    _market_data_provider = _default_market_data_provider()
    _event_provider = _default_event_provider()
    _occupancy_provider = _default_occupancy_provider()
    _channel_manager_provider = _default_channel_manager_provider()
    _billing_provider = _default_billing_provider()
