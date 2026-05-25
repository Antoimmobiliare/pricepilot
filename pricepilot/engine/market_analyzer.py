"""
PricePilot - Market Analyzer
Analisi competitiva del mercato attorno a una proprietà.

Genera competitor simulati con dati realistici e calcola
statistiche di mercato: avg, min, max, std.

In produzione sostituire simulate_competitors() con chiamate a:
  - AirDNA Market Minder API
  - Mashvisor API
  - PriceLabs Market Dashboard Export
"""
import math
import hashlib
import statistics
from datetime import date, datetime
from typing import List, Dict, Optional

from pricepilot.core.database import (
    get_property, get_properties, save_market_history,
    get_current_price_for_date,
)
from pricepilot.engine.location_context import (
    detect_location_context, get_context_adjustment,
)


# ─── Profili competitor realistici ───────────────────────────────────────────
_COMPETITOR_PROFILES = [
    {"name": "Cozy Studio Downtown",      "price_mult": 0.88, "beds": 1, "rating": 4.7, "distance_km": 0.3},
    {"name": "Modern Loft City Center",   "price_mult": 1.15, "beds": 2, "rating": 4.9, "distance_km": 0.5},
    {"name": "Charming Flat Old Town",    "price_mult": 0.95, "beds": 1, "rating": 4.5, "distance_km": 0.7},
    {"name": "Luxury Suite Rooftop",      "price_mult": 1.40, "beds": 2, "rating": 4.8, "distance_km": 0.4},
    {"name": "Budget Room Near Station",  "price_mult": 0.70, "beds": 1, "rating": 4.2, "distance_km": 1.2},
    {"name": "Riverside Apartment",       "price_mult": 1.05, "beds": 2, "rating": 4.6, "distance_km": 0.8},
    {"name": "Garden Terrace Studio",     "price_mult": 0.92, "beds": 1, "rating": 4.4, "distance_km": 1.0},
    {"name": "Business District Suite",   "price_mult": 1.28, "beds": 2, "rating": 4.9, "distance_km": 0.6},
    {"name": "Artist Quarter Loft",       "price_mult": 0.98, "beds": 1, "rating": 4.7, "distance_km": 0.9},
    {"name": "Harbour View Apartment",    "price_mult": 1.18, "beds": 3, "rating": 4.6, "distance_km": 0.5},
    {"name": "Quiet Neighbourhood Flat",  "price_mult": 0.82, "beds": 2, "rating": 4.3, "distance_km": 1.5},
    {"name": "Central Penthouse",         "price_mult": 1.55, "beds": 3, "rating": 5.0, "distance_km": 0.2},
]

_SEASONAL = {
    1: 0.78, 2: 0.80, 3: 0.90, 4: 0.98,
    5: 1.05, 6: 1.12, 7: 1.28, 8: 1.35,
    9: 1.08, 10: 0.95, 11: 0.82, 12: 1.18,
}


def _noise(seed_str: str, spread: float = 0.07) -> float:
    h = int(hashlib.md5(seed_str.encode()).hexdigest(), 16)
    return ((h % 1000) / 1000.0 - 0.5) * 2 * spread


def simulate_competitors(
    base_price: float,
    target_date: date = None,
    event: str = "",
    n: int = 10,
    context_type: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
) -> List[Dict]:
    """
    Genera n competitor simulati con dati realistici.

    Args:
        base_price:   Prezzo base della proprietà.
        target_date:  Data target (default: oggi).
        event:        Tipo di evento locale (es. "festival").
        n:            Numero di competitor da simulare.
        context_type: Tipo di contesto geografico (es. "beach_resort").
                      Se None, viene rilevato da lat/lon o usa il default.
        lat, lon:     Coordinate per auto-detection del contesto.

    Returns:
        Lista di dict con: name, price, beds, rating, distance_km, occupancy
    """
    d = target_date or date.today()
    profiles = _COMPETITOR_PROFILES[:n]

    season_mult = _SEASONAL.get(d.month, 1.0)
    is_wknd     = d.weekday() >= 4
    dow_mult    = 1.18 if is_wknd else 1.0
    evt_mult    = _event_mult(event)

    # Aggiustamento contesto geografico
    ctx         = detect_location_context(lat=lat, lon=lon, context_type=context_type)
    ctx_mult    = get_context_adjustment(ctx, is_weekend=is_wknd, has_event=bool(event))

    result = []
    for p in profiles:
        noise_key = f"{p['name']}_{d.isoformat()}"
        price = round(
            base_price * p["price_mult"] * season_mult * dow_mult * evt_mult * ctx_mult
            * (1 + _noise(noise_key, 0.08)),
            2,
        )
        price = max(price, 20.0)

        # Occupancy: inversamente correlata al prezzo relativo
        base_occ  = 0.60 + _noise(f"occ_{noise_key}", 0.25)
        rel_price = price / max(base_price, 1)
        occupancy = round(max(0.05, min(0.98, base_occ - (rel_price - 1) * 0.12)), 2)

        result.append({
            "name":        p["name"],
            "price":       price,
            "beds":        p["beds"],
            "rating":      p["rating"],
            "distance_km": p["distance_km"],
            "occupancy":   occupancy,
        })

    return sorted(result, key=lambda x: x["price"])


def calculate_market_stats(competitors: List[Dict]) -> Dict:
    """
    Calcola statistiche di mercato da lista competitor.

    Returns:
        market_avg, market_min, market_max, market_std,
        competitor_count, median_price
    """
    if not competitors:
        return {
            "market_avg": 0.0, "market_min": 0.0, "market_max": 0.0,
            "market_std": 0.0, "competitor_count": 0, "median_price": 0.0,
        }

    prices = sorted(c["price"] for c in competitors)
    n      = len(prices)
    avg    = statistics.mean(prices)
    std    = statistics.stdev(prices) if n > 1 else 0.0
    median = statistics.median(prices)

    return {
        "market_avg":       round(avg, 2),
        "market_min":       round(prices[0], 2),
        "market_max":       round(prices[-1], 2),
        "market_std":       round(std, 2),
        "competitor_count": n,
        "median_price":     round(median, 2),
    }


def get_market_competitors(
    property_id: int = 1,
    target_date: date = None,
    event: str = "",
    n: int = 10,
) -> List[Dict]:
    """
    Ritorna competitor per una proprietà dal database o simulati.
    Entry point principale per il Decision Engine.
    """
    d = target_date or date.today()
    prop = get_property(property_id)
    base_price = get_current_price_for_date(prop, d.isoformat())[0] if prop else 80.0
    return simulate_competitors(
        base_price,
        d,
        event,
        n,
        lat=prop.get("latitude") if prop else None,
        lon=prop.get("longitude") if prop else None,
    )


def run_market_analysis(
    property_id: int,
    target_date: date = None,
    event: str = "",
    competitor_count: int = 10,
    persist: bool = True,
    account_id: int = 1,
    source: str = "demo",
) -> Dict:
    """
    Esegue analisi completa: simula competitor, calcola stats, salva su DB.
    Ritorna dict con competitors + market_stats.
    """
    d = target_date or date.today()
    competitors = get_market_competitors(property_id, d, event, competitor_count)
    stats = calculate_market_stats(competitors)

    if persist:
        save_market_history({
            "account_id":       account_id,
            "property_id":      property_id,
            "date":             d.isoformat(),
            "market_avg":       stats["market_avg"],
            "market_min":       stats["market_min"],
            "market_max":       stats["market_max"],
            "market_std":       stats["market_std"],
            "competitor_count": stats["competitor_count"],
            "source":           source,
        })

    return {
        "competitors":   competitors,
        "market_stats":  stats,
        "date":          d.isoformat(),
        "property_id":   property_id,
        "account_id":    account_id,
        "source":        source,
    }


def _event_mult(event: str) -> float:
    BOOSTS = {
        "conference": 1.28, "festival": 1.30, "holiday": 1.22,
        "concert": 1.25, "marathon": 1.15, "local_fair": 1.12,
        "fair": 1.10, "exhibition": 1.10, "market": 1.05,
    }
    return BOOSTS.get((event or "").lower().strip(), 1.0)
