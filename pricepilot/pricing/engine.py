"""
PricePilot - Pricing Engine (legacy CLI) v2
Motore di calcolo prezzi con demand_factor e guardrail avanzati.

Formula di pricing:
  market_price = media pesata (base_price * (1-w) + competitor_avg * w)
  demand_factor = occ_factor + event_factor
  working_price = market_price * (1 + demand_factor) * strategy.base_multiplier
  final_price = apply_all_safety(working_price, ...)
"""
import logging
from datetime import date, datetime
from typing import Dict, List, Optional, Any

from pricepilot.core.config import CONFIG
from pricepilot.pricing.strategies import get_strategy
from pricepilot.pricing.safety import apply_all_safety, competitor_sanity_check

logger = logging.getLogger("pricepilot.engine")

EVENT_IMPACT = {
    "high": 1.30, "medium": 1.18, "low": 1.08, "none": 1.00,
}
EVENT_TYPE_MAP = {
    "conference": "high", "festival": "high", "holiday": "high", "concert": "high",
    "marathon": "medium", "local_fair": "medium", "fair": "medium", "exhibition": "medium",
    "market": "low", "none": "none", "0": "none", "": "none",
}


def _parse_event_impact(event_str: str) -> float:
    if not event_str:
        return 1.0
    key = event_str.lower().strip()
    if key in ("none", "0", ""):
        return 1.0
    level = EVENT_TYPE_MAP.get(key, "low")
    return EVENT_IMPACT.get(level, 1.08)


def _market_stats(competitor_prices: List[float]) -> Dict[str, float]:
    if not competitor_prices:
        return {"avg": 0.0, "min": 0.0, "max": 0.0, "count": 0}
    return {
        "avg":   round(sum(competitor_prices) / len(competitor_prices), 2),
        "min":   round(min(competitor_prices), 2),
        "max":   round(max(competitor_prices), 2),
        "count": len(competitor_prices),
    }


def _market_position(our_price: float, market_avg: float) -> str:
    if market_avg <= 0:
        return "unknown"
    ratio = our_price / market_avg
    if ratio < 0.90:
        return "below_market"
    if ratio > 1.10:
        return "above_market"
    return "at_market"


def _compute_demand_factor(
    occupancy: float, evt_base_mult: float,
    occ_low: float, occ_high: float, occ_low_m: float, occ_high_m: float,
    occ_sensitivity: float, evt_boost_scale: float,
) -> float:
    """
    demand_factor = occ_factor + event_factor (formula additiva).
    Il prezzo finale e: market_price * (1 + demand_factor) * strategy.base_multiplier
    """
    if occupancy < occ_low:
        occ_factor = (occ_low_m - 1.0) * occ_sensitivity
    elif occupancy > occ_high:
        occ_factor = (occ_high_m - 1.0) * occ_sensitivity
    else:
        ratio = (occupancy - occ_low) / max(occ_high - occ_low, 0.01)
        occ_factor = ratio * (occ_high_m - 1.0) * occ_sensitivity

    evt_factor = (evt_base_mult - 1.0) * (evt_boost_scale / 1.20) if evt_base_mult > 1.0 else 0.0
    return occ_factor + evt_factor


def calculate_price(
    base_price: float,
    competitor_prices: List[float],
    occupancy: float,
    event: str = "",
    strategy_name: Optional[str] = None,
    cfg: Optional[Dict] = None,
    target_date: Optional[date] = None,
    break_even: float = 0.0,
) -> Dict[str, Any]:
    """
    Calcola il prezzo ottimale con demand_factor e guardrail v2.

    Args:
        base_price:         Prezzo attuale della proprieta.
        competitor_prices:  Lista prezzi competitor.
        occupancy:          Tasso occupancy [0.0 - 1.0].
        event:              Stringa evento (es. "conference", "none").
        strategy_name:      Nome strategia (override config).
        cfg:                Config dict (usa CONFIG globale se None).
        target_date:        Data target (per last-minute/early-booking guardrail).
        break_even:         Prezzo minimo operativo (0 = disabilitato).

    Returns:
        Dict con tutti i dettagli della decisione.
    """
    cfg      = cfg or CONFIG
    strategy = get_strategy(strategy_name or cfg.get("strategy", "balanced"))
    d        = target_date or date.today()

    stats      = _market_stats(competitor_prices)
    market_avg = stats["avg"]
    comp_count = stats["count"]

    # Competitor sanity check: ignora dati anomali
    is_sane, sanity_note = competitor_sanity_check(
        base_price, market_avg,
        max_deviation=float(cfg.get("competitor_max_deviation", 0.60)),
    )
    if not is_sane:
        logger.warning(f"Competitor sanity FAIL: {sanity_note}. Uso solo base_price.")
        market_avg = 0.0

    # Market price (anchor pesato)
    if market_avg > 0:
        w = strategy.competitor_weight
        market_price = base_price * (1 - w) + market_avg * w
    else:
        market_price = base_price

    occ_low    = float(cfg.get("occupancy_low_threshold",  0.30))
    occ_high   = float(cfg.get("occupancy_high_threshold", 0.80))
    occ_low_m  = float(cfg.get("occupancy_low_multiplier",  0.90))
    occ_high_m = float(cfg.get("occupancy_high_multiplier", 1.15))

    evt_base_mult = _parse_event_impact(event)
    demand_factor = _compute_demand_factor(
        occupancy, evt_base_mult,
        occ_low, occ_high, occ_low_m, occ_high_m,
        strategy.occupancy_sensitivity, strategy.event_boost,
    )

    # Formula: market_price * (1 + demand_factor) * base_multiplier
    working_price = market_price * (1 + demand_factor) * strategy.base_multiplier

    # Safety rules v2 (complete con guardrail avanzati)
    days_until = max(0, (d - date.today()).days) if d >= date.today() else 0
    has_event  = bool(event and event.lower() not in ("none", "0", ""))
    is_weekend = d.weekday() in (4, 5)

    final_price, safety_note = apply_all_safety(
        old_price         = base_price,
        new_price         = working_price,
        min_price         = float(cfg.get("min_price_per_night", 50)),
        max_price         = float(cfg.get("max_price_per_night", 500)),
        max_change_pct    = float(cfg.get("max_change_pct", 0.20)),
        break_even        = break_even,
        days_until        = days_until,
        competitor_avg    = market_avg,
        is_weekend        = is_weekend,
        has_event         = has_event,
        occupancy         = occupancy,
        occ_high_threshold= occ_high,
    )

    pct_change = round((final_price - base_price) / max(base_price, 1), 4)
    position   = _market_position(final_price, market_avg)

    decision_parts = []
    if market_avg > 0:
        decision_parts.append(f"market_avg={market_avg:.0f}")
    if occupancy < occ_low:
        decision_parts.append("low_occupancy")
    elif occupancy > occ_high:
        decision_parts.append("high_occupancy")
    if demand_factor != 0:
        decision_parts.append(f"demand={demand_factor:.3f}")
    if evt_base_mult > 1.0:
        decision_parts.append(f"event({event})")
    decision_parts.append(f"strategy={strategy.name}")
    if safety_note != "ok":
        decision_parts.append(f"safety:{safety_note}")

    return {
        "timestamp":        datetime.utcnow().isoformat(),
        "old_price":        base_price,
        "new_price":        final_price,
        "pct_change":       pct_change,
        "competitor_price": market_avg,
        "market_price":     market_avg,
        "competitor_count": comp_count,
        "competitor_min":   stats["min"],
        "competitor_max":   stats["max"],
        "occupancy":        occupancy,
        "event":            event,
        "strategy":         strategy.name,
        "decision":         " | ".join(decision_parts),
        "market_position":  position,
        "occ_multiplier":   round(1 + demand_factor, 4),
        "evt_multiplier":   round(evt_base_mult, 4),
        "demand_factor":    round(demand_factor, 4),
        "safety_note":      safety_note,
        "days_until":       days_until,
    }
