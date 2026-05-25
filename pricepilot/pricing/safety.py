"""
PricePilot - Safety Rules (v2)
Guardrails avanzati per pricing sicuro e robusto.

Regole applicate in sequenza da apply_all_safety():
  1. Max change % per aggiornamento
  2. Last-minute guardrail  (<3 giorni: max -10%)
  3. Early booking guardrail(>60 giorni: max -5%)
  4. Break-even guardrail   (non scendere sotto i costi)
  5. Dynamic floor          (floor adattivo per weekend/eventi/alta occ.)
  6. Floor statico          (min_price assoluto)
  7. Ceiling                (max_price assoluto)

Funzione standalone (pre-validazione):
  competitor_sanity_check() -> se competitor_avg e fuori range +/-60%
"""
import logging
from typing import Tuple

logger = logging.getLogger("pricepilot.safety")


def enforce_floor(price: float, min_price: float) -> float:
    if price < min_price:
        logger.debug(f"Floor: {price:.2f} -> {min_price:.2f}")
        return min_price
    return price


def enforce_ceiling(price: float, max_price: float) -> float:
    if price > max_price:
        logger.debug(f"Ceiling: {price:.2f} -> {max_price:.2f}")
        return max_price
    return price


def enforce_max_change(old_price: float, new_price: float, max_pct: float) -> Tuple[float, str]:
    """Limita la variazione massima per singolo aggiornamento."""
    if old_price <= 0:
        return new_price, "base_price_zero"
    low  = old_price * (1 - max_pct)
    high = old_price * (1 + max_pct)
    if new_price < low:
        clamped = round(low, 2)
        note = f"clamped_down ({old_price:.2f}->{clamped:.2f}, max -{max_pct*100:.0f}%)"
        logger.debug(note)
        return clamped, note
    if new_price > high:
        clamped = round(high, 2)
        note = f"clamped_up ({old_price:.2f}->{clamped:.2f}, max +{max_pct*100:.0f}%)"
        logger.debug(note)
        return clamped, note
    return round(new_price, 2), "ok"


def enforce_break_even(price: float, break_even: float) -> Tuple[float, str]:
    """Il prezzo non puo mai scendere sotto il break-even. break_even=0 -> disabilitato."""
    if break_even <= 0:
        return price, "ok"
    if price < break_even:
        logger.warning(f"Break-even guardrail: {price:.2f} -> {break_even:.2f}")
        return round(break_even, 2), f"break_even ({break_even:.2f})"
    return price, "ok"


def enforce_last_minute(
    old_price: float, new_price: float, days_until: int,
    max_reduction_pct: float = 0.10, threshold_days: int = 3,
) -> Tuple[float, str]:
    """Se mancano <threshold_days giorni: max riduzione max_reduction_pct."""
    if days_until >= threshold_days or old_price <= 0:
        return new_price, "ok"
    floor_lm = old_price * (1 - max_reduction_pct)
    if new_price < floor_lm:
        clamped = round(floor_lm, 2)
        note = f"last_minute ({days_until}d, max -{max_reduction_pct*100:.0f}%): {old_price:.2f}->{clamped:.2f}"
        logger.info(note)
        return clamped, note
    return new_price, "ok"


def enforce_early_booking(
    old_price: float, new_price: float, days_until: int,
    threshold_days: int = 60, max_reduction_pct: float = 0.05,
) -> Tuple[float, str]:
    """Se data e oltre threshold_days giorni: evitare riduzioni aggressive."""
    if days_until <= threshold_days or old_price <= 0:
        return new_price, "ok"
    floor_eb = old_price * (1 - max_reduction_pct)
    if new_price < floor_eb:
        clamped = round(floor_eb, 2)
        note = f"early_booking ({days_until}d, max -{max_reduction_pct*100:.0f}%): {old_price:.2f}->{clamped:.2f}"
        logger.info(note)
        return clamped, note
    return new_price, "ok"


def competitor_sanity_check(
    old_price: float, competitor_avg: float, max_deviation: float = 0.60,
) -> Tuple[bool, str]:
    """
    Verifica che competitor_avg non sia anomalo (>max_deviation dal prezzo attuale).
    Ritorna (is_sane, note). is_sane=False -> non applicare il cambiamento.
    """
    if competitor_avg <= 0 or old_price <= 0:
        return True, "ok"
    deviation = abs(competitor_avg - old_price) / old_price
    if deviation > max_deviation:
        note = (f"competitor_outlier: cur={old_price:.2f} comp_avg={competitor_avg:.2f} "
                f"dev={deviation*100:.0f}% > {max_deviation*100:.0f}%")
        logger.warning(f"[SANITY] {note}")
        return False, note
    return True, "ok"


def compute_dynamic_floor(
    min_price: float, is_weekend: bool = False, has_event: bool = False,
    occupancy: float = 0.0, occ_high_threshold: float = 0.80,
    weekend_boost: float = 0.10, event_boost: float = 0.20, occ_boost: float = 0.15,
) -> float:
    """
    Floor dinamico: aumenta automaticamente in base al contesto.
      weekend        -> +10%
      evento         -> +20%
      alta occupancy -> +15%
    I boost si sommano.
    """
    total_boost = 0.0
    reasons = []
    if is_weekend:
        total_boost += weekend_boost
        reasons.append(f"weekend+{weekend_boost*100:.0f}%")
    if has_event:
        total_boost += event_boost
        reasons.append(f"event+{event_boost*100:.0f}%")
    if occupancy >= occ_high_threshold:
        total_boost += occ_boost
        reasons.append(f"high_occ+{occ_boost*100:.0f}%")
    dynamic = round(min_price * (1 + total_boost), 2)
    if total_boost > 0:
        logger.debug(f"Dynamic floor: {min_price:.2f} -> {dynamic:.2f} ({', '.join(reasons)})")
    return dynamic


def apply_all_safety(
    old_price: float,
    new_price: float,
    min_price: float,
    max_price: float,
    max_change_pct: float,
    break_even: float = 0.0,
    days_until: int = 999,
    competitor_avg: float = 0.0,
    is_weekend: bool = False,
    has_event: bool = False,
    occupancy: float = 0.0,
    occ_high_threshold: float = 0.80,
) -> Tuple[float, str]:
    """
    Applica tutti i guardrail in sequenza (backward-compatible, parametri extra opzionali).

    Ordine: max_change -> last_minute -> early_booking -> break_even
            -> dynamic_floor -> floor_statico -> ceiling
    """
    notes = []

    price, note = enforce_max_change(old_price, new_price, max_change_pct)
    if note != "ok":
        notes.append(note)

    price, note = enforce_last_minute(old_price, price, days_until)
    if note != "ok":
        notes.append(note)

    price, note = enforce_early_booking(old_price, price, days_until)
    if note != "ok":
        notes.append(note)

    price, note = enforce_break_even(price, break_even)
    if note != "ok":
        notes.append(note)

    dyn_floor = compute_dynamic_floor(min_price, is_weekend, has_event, occupancy, occ_high_threshold)
    effective_floor = max(min_price, dyn_floor)
    if price < effective_floor:
        price = effective_floor
        notes.append(f"dynamic_floor={effective_floor:.2f}")

    price = enforce_floor(price, min_price)
    price = enforce_ceiling(price, max_price)

    return round(price, 2), (" | ".join(notes) if notes else "ok")
