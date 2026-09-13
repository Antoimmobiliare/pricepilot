"""Market-provider building blocks for real competitor integrations.

PricePilot must never present synthetic competitors as real market data in a
cloud environment. Until a channel manager or a licensed market-data source
is connected, this provider makes that state explicit while allowing the
engine to continue creating advisory decisions from the other signals.
"""
from __future__ import annotations

from datetime import date

from pricepilot.providers.contracts import MarketDataResult
from pricepilot.core.data_quality import DataUnavailable


class UnconfiguredOccupancyProvider:
    name = "occupancy_provider_unconfigured"

    def estimate(self, **kwargs):
        raise DataUnavailable("Calendario non collegato: occupazione sconosciuta.")


class UnconfiguredCompetitorProvider:
    """Safe placeholder for production before a real market source exists."""

    name = "competitor_provider_unconfigured"

    def analyze(
        self,
        *,
        property_id: int,
        target_date: date,
        event: str = "",
        competitor_count: int = 10,
        account_id: int = 1,
        source: str = "unconfigured",
        persist: bool = True,
    ) -> MarketDataResult:
        return MarketDataResult(
            competitors=[],
            market_stats={
                "market_avg": 0.0,
                "market_min": 0.0,
                "market_max": 0.0,
                "market_std": 0.0,
                "competitor_count": 0,
                "median_price": 0.0,
            },
            source=self.name,
            raw={
                "status": "not_configured",
                "property_id": property_id,
                "date": target_date.isoformat(),
                "next_step": "Connect a channel manager or a licensed market-data provider.",
            },
        )
