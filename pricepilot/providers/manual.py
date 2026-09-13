"""
Manual CSV providers for pre-integration testing.

These providers let PricePilot run with real data collected manually before
buying market-data, events, PMS, or channel-manager tools. They use the same
contracts as real integrations, so the pricing engine does not change later.
"""
from __future__ import annotations

import csv
import os
import statistics
from datetime import date
from pathlib import Path
from typing import Optional

from pricepilot.providers.contracts import MarketDataResult, OccupancyResult


def _path_from_env(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default).strip() or default)


def _read_csv(path: Path) -> list[dict]:
    if not path.exists() or not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def _matches_scope(row: dict, *, account_id: int, property_id: int, target_date: date) -> bool:
    row_date = str(row.get("date") or "").strip()
    if row_date and row_date != target_date.isoformat():
        return False

    row_account = str(row.get("account_id") or "").strip()
    if row_account and row_account != str(account_id):
        return False

    row_property = str(row.get("property_id") or row.get("property_local_id") or "").strip()
    if row_property and row_property != str(property_id):
        return False

    return True


def _float_value(value, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _market_stats(competitors: list[dict]) -> dict:
    prices = sorted(float(c["price"]) for c in competitors if _float_value(c.get("price")) > 0)
    if not prices:
        return {
            "market_avg": 0.0,
            "market_min": 0.0,
            "market_max": 0.0,
            "market_std": 0.0,
            "competitor_count": 0,
            "median_price": 0.0,
        }

    return {
        "market_avg": round(statistics.mean(prices), 2),
        "market_min": round(prices[0], 2),
        "market_max": round(prices[-1], 2),
        "market_std": round(statistics.stdev(prices), 2) if len(prices) > 1 else 0.0,
        "competitor_count": len(prices),
        "median_price": round(statistics.median(prices), 2),
    }


class ManualMarketDataProvider:
    name = "manual_csv_market"

    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path) if path else _path_from_env(
            "PRICEPILOT_MANUAL_MARKET_CSV",
            "data/manual_market.csv",
        )

    def analyze(
        self,
        *,
        property_id: int,
        target_date: date,
        event: str = "",
        competitor_count: int = 10,
        account_id: int = 1,
        source: str = "manual_csv",
        persist: bool = True,
    ) -> MarketDataResult:
        rows = _read_csv(self.path)
        competitors: list[dict] = []

        for row in rows:
            if not _matches_scope(row, account_id=account_id, property_id=property_id, target_date=target_date):
                continue
            price = _float_value(row.get("price") or row.get("competitor_price"))
            if price <= 0:
                continue
            competitors.append({
                "name": row.get("competitor_name") or row.get("name") or f"Competitor {len(competitors) + 1}",
                "price": price,
                "platform": row.get("platform") or "",
                "distance_km": _float_value(row.get("distance_km"), 0.0),
                "raw": row,
            })
            if len(competitors) >= int(competitor_count or 10):
                break

        stats = _market_stats(competitors)
        if persist:
            from pricepilot.core.database import save_market_history

            save_market_history({
                "account_id": account_id,
                "property_id": property_id,
                "date": target_date.isoformat(),
                "market_avg": stats["market_avg"],
                "market_min": stats["market_min"],
                "market_max": stats["market_max"],
                "market_std": stats["market_std"],
                "competitor_count": stats["competitor_count"],
                "source": self.name,
            })

        return MarketDataResult(
            competitors=competitors,
            market_stats=stats,
            source=self.name if competitors else "manual_csv_empty",
            raw={"path": str(self.path), "rows": len(rows), "event": event},
        )


class ManualEventProvider:
    name = "manual_csv_events"

    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path) if path else _path_from_env(
            "PRICEPILOT_MANUAL_EVENTS_CSV",
            "data/manual_events.csv",
        )

    def event_for_date(self, target_date: date) -> Optional[dict]:
        rows = [
            row for row in _read_csv(self.path)
            if str(row.get("date") or "").strip() == target_date.isoformat()
        ]
        if not rows:
            return None
        priority = {"high": 3, "medium": 2, "low": 1}
        return max(rows, key=lambda row: priority.get(str(row.get("impact_level") or "").lower(), 0))

    def event_for_property(
        self,
        *,
        prop: dict,
        target_date: date,
        account_id: int = 1,
    ) -> Optional[dict]:
        return self.event_for_date(target_date)

    def event_to_string(self, event: Optional[dict]) -> str:
        if not event:
            return "none"
        return str(event.get("event_type") or event.get("name") or "event").strip().lower()


class ManualOccupancyProvider:
    name = "manual_csv_occupancy"

    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path) if path else _path_from_env(
            "PRICEPILOT_MANUAL_OCCUPANCY_CSV",
            "data/manual_occupancy.csv",
        )

    def estimate(
        self,
        *,
        property_id: int,
        target_date: date,
        account_id: int = 1,
    ) -> OccupancyResult:
        for row in _read_csv(self.path):
            if not _matches_scope(row, account_id=account_id, property_id=property_id, target_date=target_date):
                continue
            occupancy = _float_value(row.get("occupancy"))
            if occupancy > 1:
                occupancy = occupancy / 100
            occupancy = min(1.0, max(0.0, occupancy))
            return OccupancyResult(
                occupancy=occupancy,
                source=self.name,
                raw={"path": str(self.path), "row": row},
            )

        fallback = _float_value(os.environ.get("PRICEPILOT_MANUAL_DEFAULT_OCCUPANCY", "0.55"), 0.55)
        return OccupancyResult(
            occupancy=min(1.0, max(0.0, fallback)),
            source="manual_default_occupancy",
            raw={"path": str(self.path), "matched": False},
        )
