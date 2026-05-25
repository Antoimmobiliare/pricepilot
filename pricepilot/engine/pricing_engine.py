"""
PricePilot - Pricing Engine v2
Motore di calcolo prezzi per la dashboard e il Decision Engine.

Regole di business:
  1. Prezzo di partenza: market_avg (o base_price se non disponibile)
  2. Occupancy > occ_high  -> +boost
  3. Weekend (ven/sab)     -> +15%
  4. Evento presente       -> +20%
  5. season_factor manuale -> moltiplicatore
  6. Safety rules v2 (floor, ceiling, max change, last-minute, early-booking,
                      break-even, dynamic floor, competitor sanity)
"""
from datetime import date
from typing import Dict, Optional

from pricepilot.pricing.safety import (
    apply_all_safety, competitor_sanity_check, compute_dynamic_floor,
)

OCC_HIGH_THRESHOLD  = 0.75
OCC_LOW_THRESHOLD   = 0.40
OCC_HIGH_BOOST      = 1.10
OCC_LOW_DISCOUNT    = 0.90
WEEKEND_BOOST       = 1.15
EVENT_BOOST         = 1.20


def is_weekend(target_date: date) -> bool:
    """Venerdi (4) e sabato (5) sono considerati weekend per gli affitti brevi."""
    return target_date.weekday() in (4, 5)


def calculate_recommended_price(
    base_price: float,
    market_avg: float,
    occupancy: float,
    target_date: date = None,
    has_event: bool = False,
    min_price: float = 50.0,
    max_price: float = 500.0,
    competitor_count: int = 0,
    season_factor: float = 1.0,
    event_factor: float = 1.0,
    # Nuovi parametri v2 guardrail (opzionali)
    days_until: int = 999,
    break_even: float = 0.0,
    competitor_avg: float = 0.0,
    occ_high_threshold: float = 0.80,
    max_change_pct: float = 0.20,
) -> Dict:
    """
    Calcola il prezzo raccomandato con guardrail v2 completi.

    Args:
        base_price:       Prezzo attuale della proprieta.
        market_avg:       Media prezzi competitor (0 = usa base_price).
        occupancy:        Tasso occupancy [0.0 - 1.0].
        target_date:      Data target (default: oggi).
        has_event:        True se esiste un evento locale.
        min_price:        Prezzo minimo assoluto.
        max_price:        Prezzo massimo assoluto.
        competitor_count: Numero di competitor analizzati.
        season_factor:    Moltiplicatore stagionale manuale [0.5 - 2.0].
        event_factor:     Moltiplicatore evento manuale [1.0 - 2.0].
        days_until:       Giorni alla data target (per last-minute/early-booking).
        break_even:       Prezzo minimo operativo (0 = disabilitato).
        competitor_avg:   Media competitor per sanity check.
        occ_high_threshold: Soglia alta occupancy.
        max_change_pct:  Variazione massima consentita per singolo ciclo.

    Returns:
        Dict con recommended_price, delta_vs_market, delta_vs_base,
              confidence_score, breakdown, guardrail info.
    """
    d = target_date or date.today()

    # Calcola days_until se non fornito
    if days_until == 999 and d >= date.today():
        days_until = (d - date.today()).days

    # 1. Prezzo di partenza
    price = market_avg if market_avg > 0 else base_price
    breakdown = {"start": round(price, 2)}

    # 2. Occupancy
    if occupancy > OCC_HIGH_THRESHOLD:
        price *= OCC_HIGH_BOOST
        breakdown["occupancy_boost"] = f"+{(OCC_HIGH_BOOST-1)*100:.0f}% (occ {occupancy:.0%})"
    elif occupancy < OCC_LOW_THRESHOLD:
        price *= OCC_LOW_DISCOUNT
        breakdown["occupancy_discount"] = f"{(OCC_LOW_DISCOUNT-1)*100:.0f}% (occ {occupancy:.0%})"
    else:
        breakdown["occupancy"] = f"neutro ({occupancy:.0%})"

    # 3. Weekend
    weekend = is_weekend(d)
    if weekend:
        price *= WEEKEND_BOOST
        breakdown["weekend_boost"] = f"+{(WEEKEND_BOOST-1)*100:.0f}%"

    # 4. Evento
    if has_event:
        price *= EVENT_BOOST * event_factor
        breakdown["event_boost"] = f"+{(EVENT_BOOST*event_factor-1)*100:.0f}%"

    # 5. Stagionalita manuale
    if season_factor != 1.0:
        price *= season_factor
        breakdown["season_factor"] = f"x{season_factor:.2f}"

    # 6. Safety rules v2 (completo)
    comp_for_check = competitor_avg if competitor_avg > 0 else market_avg
    final_price, safety_note = apply_all_safety(
        old_price         = base_price,
        new_price         = price,
        min_price         = min_price,
        max_price         = max_price,
        max_change_pct    = max_change_pct,
        break_even        = break_even,
        days_until        = days_until,
        competitor_avg    = comp_for_check,
        is_weekend        = weekend,
        has_event         = has_event,
        occupancy         = occupancy,
        occ_high_threshold= occ_high_threshold,
    )

    if safety_note != "ok":
        breakdown["safety"] = safety_note

    price = final_price

    # Delta e confidence
    delta_vs_market = round(
        (price - market_avg) / market_avg * 100 if market_avg > 0 else 0, 2
    )
    delta_vs_base = round(
        (price - base_price) / base_price * 100 if base_price > 0 else 0, 2
    )

    confidence = _confidence_score(competitor_count, market_avg, occupancy)

    # Dynamic floor info (per visualizzazione dashboard)
    dyn_floor = compute_dynamic_floor(
        min_price, weekend, has_event, occupancy, occ_high_threshold
    )

    return {
        "recommended_price":  price,
        "delta_vs_market":    delta_vs_market,
        "delta_vs_base":      delta_vs_base,
        "confidence_score":   confidence,
        "is_weekend":         weekend,
        "has_event":          has_event,
        "breakdown":          breakdown,
        "min_price":          min_price,
        "max_price":          max_price,
        "safety_note":        safety_note,
        "dynamic_floor":      dyn_floor,
        "days_until":         days_until,
    }


def _confidence_score(competitor_count: int, market_avg: float, occupancy: float) -> float:
    """Score da 0.0 a 1.0. Piu competitor e occupancy nota -> score piu alto."""
    score = 0.4
    if market_avg > 0:
        score += 0.3
    if competitor_count >= 5:
        score += 0.2
    elif competitor_count >= 2:
        score += 0.1
    if 0.0 < occupancy <= 1.0:
        score += 0.1
    return round(min(score, 1.0), 2)
