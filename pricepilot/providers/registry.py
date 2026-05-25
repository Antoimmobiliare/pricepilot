"""
Provider registry for PricePilot.

Production integrations should be registered here at app startup. The rest of
the codebase reads providers through these getters.
"""
from __future__ import annotations

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


_market_data_provider: MarketDataProvider = DemoMarketDataProvider()
_event_provider: EventProvider = DemoEventProvider()
_occupancy_provider: OccupancyProvider = DemoOccupancyProvider()
_channel_manager_provider: ChannelManagerProvider = DefaultChannelManagerProvider()
_billing_provider: BillingProvider = LocalBillingProvider()


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

    _market_data_provider = DemoMarketDataProvider()
    _event_provider = DemoEventProvider()
    _occupancy_provider = DemoOccupancyProvider()
    _channel_manager_provider = DefaultChannelManagerProvider()
    _billing_provider = LocalBillingProvider()
