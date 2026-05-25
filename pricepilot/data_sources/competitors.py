"""
PricePilot - Competitor Data Source
Simulatore realistico di dati competitor.

In produzione questo modulo si collega a:
  - AirDNA API  (https://developer.airdna.co)
  - PriceLabs   (dati di mercato via export)
  - Wheelhouse  (https://usewheelhouse.com)
  - Scraping diretto (solo dove consentito dai T&S)

Il simulatore usa seed deterministici per avere coerenza tra
run successive sulle stesse date, imitando un vero mercato.
"""
import hashlib
import random
import math
from datetime import date, datetime
from typing import List, Dict


# ─── Stagionalità mensile ─────────────────────────────────────────────────────
SEASONAL = {
    1: 0.78, 2: 0.80, 3: 0.90, 4: 0.98,
    5: 1.05, 6: 1.12, 7: 1.28, 8: 1.35,
    9: 1.08, 10: 0.95, 11: 0.82, 12: 1.18,
}

# ─── Profili competitor ───────────────────────────────────────────────────────
COMPETITOR_PROFILES = [
    {"name": "Cozy Downtown Studio",    "mult": 0.88, "rating": 4.7, "reviews": 312},
    {"name": "Lake View Apartment",     "mult": 1.15, "rating": 4.9, "reviews": 189},
    {"name": "City Center Flat",        "mult": 0.95, "rating": 4.5, "reviews": 456},
    {"name": "Modern Loft Suite",       "mult": 1.20, "rating": 4.8, "reviews": 98},
    {"name": "Historic Quarter Room",   "mult": 0.82, "rating": 4.3, "reviews": 201},
    {"name": "Riverside Apartment",     "mult": 1.05, "rating": 4.6, "reviews": 134},
    {"name": "Garden Terrace Studio",   "mult": 0.92, "rating": 4.4, "reviews": 278},
    {"name": "Business District Suite", "mult": 1.30, "rating": 4.9, "reviews": 67},
    {"name": "Old Town Charm",          "mult": 0.78, "rating": 4.2, "reviews": 389},
    {"name": "Skyline Penthouse",       "mult": 1.45, "rating": 5.0, "reviews": 44},
    {"name": "Artist Quarter Loft",     "mult": 0.98, "rating": 4.7, "reviews": 156},
    {"name": "Harbour View Studio",     "mult": 1.10, "rating": 4.6, "reviews": 221},
]


def _deterministic_noise(seed_str: str, spread: float = 0.08) -> float:
    """
    Genera rumore deterministico [-spread, +spread] da una stringa seed.
    Stesso seed → stesso risultato (utile per coerenza tra run).
    """
    h = int(hashlib.md5(seed_str.encode()).hexdigest(), 16)
    rng = (h % 1000) / 1000.0          # [0, 1)
    return (rng - 0.5) * 2 * spread    # [-spread, +spread]


def get_competitor_prices(
    base_price: float,
    target_date: date,
    event: str = "",
    num_competitors: int = None,
) -> List[Dict]:
    """
    Restituisce prezzi simulati dei competitor per una data specifica.

    Args:
        base_price:       Prezzo base della proprietà (anchor di mercato).
        target_date:      Data target.
        event:            Stringa evento (amplifica i prezzi).
        num_competitors:  Quanti competitor simulare (default: tutti).

    Returns:
        Lista di dict con: source, property_name, price, occupancy_rate,
        rating, num_reviews, date.
    """
    profiles = COMPETITOR_PROFILES
    if num_competitors and num_competitors < len(profiles):
        # selezione deterministica basata sulla data
        day_seed = target_date.toordinal()
        rng = random.Random(day_seed)
        profiles = rng.sample(profiles, num_competitors)

    season_mult = SEASONAL.get(target_date.month, 1.0)
    dow_mult    = 1.18 if target_date.weekday() >= 4 else 1.0  # venerdì-domenica
    evt_mult    = _event_multiplier(event)

    results = []
    for p in profiles:
        noise_seed = f"{p['name']}_{target_date.isoformat()}"
        noise      = _deterministic_noise(noise_seed, spread=0.09)

        price = round(
            base_price
            * p["mult"]
            * season_mult
            * dow_mult
            * evt_mult
            * (1 + noise),
            2,
        )
        price = max(price, 30.0)

        # Occupancy simulata: più alto il prezzo relativo, minore l'occupancy
        rel_price  = price / max(base_price, 1)
        occ_base   = 0.65 + _deterministic_noise(f"occ_{noise_seed}", 0.20)
        occ_adjust = max(0.0, min(1.0, occ_base - (rel_price - 1) * 0.15))

        results.append({
            "source":         "simulated",
            "property_name":  p["name"],
            "price":          price,
            "occupancy_rate": round(occ_adjust, 2),
            "rating":         p["rating"],
            "num_reviews":    p["reviews"],
            "date":           target_date.isoformat(),
        })

    return sorted(results, key=lambda x: x["price"])


def _event_multiplier(event: str) -> float:
    BOOSTS = {
        "conference": 1.28, "festival": 1.30, "holiday": 1.22,
        "concert": 1.25,    "marathon": 1.15, "local_fair": 1.12,
        "fair": 1.10,       "exhibition": 1.10, "market": 1.05,
    }
    key = (event or "").lower().strip()
    return BOOSTS.get(key, 1.0)


def get_market_summary(competitor_data: List[Dict]) -> Dict:
    """Calcola statistiche di mercato da lista competitor."""
    if not competitor_data:
        return {"avg": 0, "min": 0, "max": 0, "count": 0, "median": 0}
    prices = sorted(c["price"] for c in competitor_data)
    n = len(prices)
    median = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2
    return {
        "avg":    round(sum(prices) / n, 2),
        "min":    round(prices[0], 2),
        "max":    round(prices[-1], 2),
        "median": round(median, 2),
        "count":  n,
    }
