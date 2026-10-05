"""Strict import of dated stay quotes. This is NOT a competitor scraper.

A future collector must supply the same contract. Never manufacture a nightly
quote by dividing a multi-night offer, or infer booked from unavailable.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import statistics
from urllib.parse import urlsplit

from pricepilot.core.config import BASE_DIR
from pricepilot.core.data_quality import DataUnavailable
from pricepilot.providers.contracts import MarketDataResult, OccupancyResult


def fresh_timestamp(value, now, max_age_hours=6):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
        age = (now - stamp).total_seconds()
        if not 0 <= age <= max_age_hours * 3600:
            raise ValueError()
    except (ValueError, TypeError):
        raise DataUnavailable("Osservazione scaduta, futura o senza timezone.") from None
    return stamp


def money(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise DataUnavailable("Importo non valido.") from None
    if not result.is_finite() or result < 0:
        raise DataUnavailable("Importo negativo o non finito.")
    return result


def read_document(path, version):
    try:
        if path.stat().st_size > 20_000_000:
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema_version") != version or not isinstance(value.get("observations"), list):
            raise ValueError()
        if any(not isinstance(row, dict) for row in value["observations"]):
            raise ValueError()
        return value["observations"]
    except (OSError, ValueError, AttributeError):
        raise DataUnavailable("File osservazioni mancante o formato non valido.") from None


class ObservedQuotesProvider:
    name = "observed_quotes"

    def __init__(self, path=None, clock=None):
        self.path = Path(path or os.getenv("PRICEPILOT_QUOTES_FILE") or BASE_DIR / "data" / "quotes.json")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def analyze_stay(self, *, account_id, property_id, check_in, nights=1, guests=2, currency="EUR", limit=10):
        if type(nights) is not int or not 1 <= nights <= 30 or type(guests) is not int or not 1 <= guests <= 30:
            raise ValueError("Durata o ospiti non validi.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Limite competitor non valido.")
        end = check_in + timedelta(days=nights)
        now = self.clock()
        selected = {}
        rejected = 0
        unavailable = set()
        for row in read_document(self.path, "pricepilot.quotes.v1"):
            # All identifiers mandatory: no wildcard data across tenants.
            if row.get("account_id") != account_id or row.get("property_id") != property_id:
                continue
            if (row.get("check_in"), row.get("check_out"), row.get("guests"), row.get("currency")) != (
                check_in.isoformat(), end.isoformat(), guests, currency):
                continue
            try:
                stamp = fresh_timestamp(row.get("observed_at"), now)
                identity = str(row["competitor_id"])
                url = urlsplit(row["source_url"])
                if not identity or url.scheme != "https" or not url.hostname or url.username or url.password:
                    raise DataUnavailable("Fonte non valida")
                if row.get("collection_method") not in {"manual_observation", "provider_api", "permitted_scrape"}:
                    raise DataUnavailable("Metodo non dichiarato")
                if not row.get("evidence_reference"):
                    raise DataUnavailable("Riferimento all'evidenza mancante")
                # Store only newest result, even if it is now unavailable.
                candidate = {"competitor_id": identity, "name": row.get("name") or identity,
                             "source_url": row["source_url"], "observed_at": stamp.isoformat(),
                             "evidence_reference": row["evidence_reference"], "status": row.get("status"),
                             "collection_method": row["collection_method"]}
                if row.get("status") == "available":
                    room = money(row["room_total"])
                    fees = money(row["fees_total"])
                    taxes = money(row["taxes_total"])
                    total = money(row["guest_total"])
                    if room <= 0 or abs(room + fees + taxes - total) > Decimal("0.01"):
                        raise DataUnavailable("Totale offerta non riconciliato")
                    if row.get("room_basis") != "after_discounts_excluding_fees_taxes":
                        raise DataUnavailable("Base prezzo non comparabile")
                    if row.get("cancellation_policy") != "standard":
                        raise DataUnavailable("Condizioni non comparabili con il profilo standard")
                    candidate.update(room_total=str(room), guest_total=str(total),
                                     average_room_per_night=str(room / nights), nights=nights)
                elif row.get("status") != "unavailable":
                    raise DataUnavailable("Esito ricerca sconosciuto")
                if identity not in selected or stamp > selected[identity][0]:
                    selected[identity] = (stamp, candidate)
            except (DataUnavailable, KeyError, TypeError, ValueError):
                rejected += 1
        quotes = [q for _, q in selected.values() if q["status"] == "available"]
        unavailable = [q["competitor_id"] for _, q in selected.values() if q["status"] == "unavailable"]
        quotes = sorted(quotes, key=lambda q: q["competitor_id"])[:limit]
        return {"quotes": quotes, "unavailable": unavailable, "rejected": rejected,
                "check_in": check_in.isoformat(), "check_out": end.isoformat(), "guests": guests,
                "currency": currency, "nights": nights, "source": self.name,
                "provenance": "imported_observations_not_independently_verified", "as_of": now.isoformat()}

    def analyze(self, *, property_id, target_date, competitor_count=10, account_id=1, **kwargs):
        guests = int(os.getenv("PRICEPILOT_MARKET_GUESTS", "2"))
        result = self.analyze_stay(account_id=account_id, property_id=property_id,
                                   check_in=target_date, guests=guests, limit=competitor_count)
        competitors = [{**q, "price": float(q["room_total"])} for q in result["quotes"]]
        prices = [q["price"] for q in competitors]
        if not prices:
            raise DataUnavailable("Nessuna offerta verificabile per una notte: non uso medie di soggiorni multipli.")
        return MarketDataResult(competitors=competitors, source=self.name,
            market_stats={"market_avg": statistics.mean(prices), "market_min": min(prices),
                          "market_max": max(prices), "market_std": statistics.pstdev(prices),
                          "median_price": statistics.median(prices), "competitor_count": len(prices)},
            raw={**result, "validated_observations": True})


class ObservedInventoryProvider:
    name = "observed_inventory"

    def __init__(self, path=None, clock=None):
        # Explicit paths are supported for offline imports/tests. Normal runtime
        # reads the canonical account-scoped operational database populated by
        # Beds24 sync; PRICEPILOT_INVENTORY_FILE is export-only compatibility.
        self.path = Path(path) if path is not None else None
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def estimate(self, *, property_id, target_date, account_id=1):
        window_end = target_date + timedelta(days=30)
        selected = {}
        if self.path is not None:
            rows = read_document(self.path, "pricepilot.inventory.v1")
        else:
            try:
                from pricepilot.services.operational_store import get_inventory_rows
                rows = get_inventory_rows(account_id, property_id, target_date, window_end)
            except Exception as exc:
                raise DataUnavailable("Archivio inventario non disponibile per l'account richiesto.") from exc
        for row in rows:
            if row.get("account_id") != account_id or row.get("property_id") != property_id:
                continue
            try:
                day = date.fromisoformat(row["date"])
                if not target_date <= day < window_end:
                    continue
                stamp = fresh_timestamp(row.get("observed_at"), self.clock())
                if row.get("state") not in {"open", "booked", "owner_blocked", "maintenance_blocked", "unavailable"}:
                    raise ValueError()
                if not row.get("source_reference"):
                    raise ValueError()
                if day in selected:
                    raise DataUnavailable("Inventario duplicato: riconciliare le fonti prima del pricing.")
                selected[day] = row
            except (KeyError, ValueError) as exc:
                raise DataUnavailable("Inventario incompleto, scaduto o ambiguo.") from exc
        if len(selected) != 30:
            raise DataUnavailable("Servono 30 giorni di inventario completo per la finestra occupancy.")
        available = sum(r["state"] in {"open", "booked"} for r in selected.values())
        booked = sum(r["state"] == "booked" for r in selected.values())
        if not available:
            raise DataUnavailable("Nessuna notte vendibile nella finestra: occupancy non definita.")
        return OccupancyResult(occupancy=booked / available, source=self.name,
            raw={"account_id": account_id, "property_id": property_id,
                 "target_date": target_date.isoformat(),
                 "observed_at": selected[target_date].get("observed_at"),
                 "target_state": selected[target_date]["state"], "available_nights": available,
                 "booked_nights": booked, "window_start": target_date.isoformat(),
                 "window_end_exclusive": window_end.isoformat(), "metric_version": "pmos.metrics.v1"})
