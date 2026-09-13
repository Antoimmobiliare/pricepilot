"""Explicit owner rules, stable reference rates, no external market requests.

Thresholds/multipliers are configuration, not learned market demand or forecasts.
Using the same reference prevents repeated six-hour cycles compounding discounts.
"""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pricepilot.core.config import BASE_DIR
from pricepilot.core.data_quality import DataUnavailable, validate_occupancy
from pricepilot.pricing.safety import apply_all_safety


def pricing_today():
    """Calendar day used for lead time, stable across local and cloud workers."""
    name = os.getenv('PRICEPILOT_TIMEZONE', 'Europe/Rome').strip()
    try:
        return datetime.now(ZoneInfo(name)).date()
    except ZoneInfoNotFoundError:
        raise DataUnavailable('Fuso orario pricing non valido.') from None


def load_policy(account_id, property_id):
    from pricepilot.services.operational_store import get_calendar_policy
    saved = get_calendar_policy(account_id, property_id)
    if saved is not None:
        if saved.get('enabled') is not True:
            raise DataUnavailable('Strategie calendario disattivate per questo appartamento.')
        validate_policy(saved)
        return saved
    path = Path(os.getenv('PRICEPILOT_CALENDAR_POLICY_FILE') or BASE_DIR / 'data' / 'calendar_policy.json')
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
        if document.get('schema_version') != 'pricepilot.calendar-policy.v1':
            raise ValueError()
        matches = [p for p in document['properties'] if p.get('account_id') == account_id
                   and p.get('property_id') == property_id and p.get('enabled') is True]
        if len(matches) != 1:
            raise ValueError()
        policy = matches[0]
        validate_policy(policy)
        return policy
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise DataUnavailable('Configurare tariffa di riferimento e regole calendario per questo appartamento.') from None


def finite(value, low, high):
    if isinstance(value, bool):
        raise ValueError('Valore numerico non valido.')
    number = float(value)
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError('Regola calendario fuori intervallo.')
    return number


def validate_policy(policy):
    finite(policy['reference_price'], .01, 100000)
    finite(policy.get('weekend_multiplier', 1), .5, 2)
    finite(policy.get('break_even', 0), 0, 100000)
    finite(policy.get('minimum_change_eur', 1), .01, 1000)
    gap = policy.get('gap_rule', {})
    if gap:
        if type(gap.get('enabled', False)) is not bool:
            raise ValueError('Attivazione regola vuoti non valida.')
        if type(gap.get('max_nights')) is not int or not 1 <= gap['max_nights'] <= 7:
            raise ValueError('Durata vuoto non valida.')
        if type(gap.get('through_days')) is not int or not 0 <= gap['through_days'] <= 60:
            raise ValueError('Anticipo vuoto non valido.')
        finite(gap.get('multiplier'), .5, 1)
    pacing = policy.get('pacing_rule', {})
    if pacing:
        if type(pacing.get('enabled', False)) is not bool:
            raise ValueError('Attivazione regola pacing non valida.')
        if type(pacing.get('through_days')) is not int or not 1 <= pacing['through_days'] <= 366:
            raise ValueError('Orizzonte pacing non valido.')
        low = pacing.get('low_pickup_7d_nights')
        high = pacing.get('high_pickup_7d_nights')
        if type(low) is not int or type(high) is not int or not 0 <= low < high <= 30:
            raise ValueError('Soglie pickup non valide.')
        finite(pacing.get('low_multiplier'), .80, 1)
        finite(pacing.get('high_multiplier'), 1, 1.20)
    minimum_stay = policy.get('minimum_stay_rule', {})
    if minimum_stay:
        if type(minimum_stay.get('enabled', False)) is not bool:
            raise ValueError('Attivazione regola soggiorno minimo non valida.')
        if type(minimum_stay.get('through_days')) is not int or not 0 <= minimum_stay['through_days'] <= 60:
            raise ValueError('Orizzonte soggiorno minimo non valido.')
        if type(minimum_stay.get('max_gap_nights')) is not int or not 1 <= minimum_stay['max_gap_nights'] <= 7:
            raise ValueError('Durata massima del vuoto non valida.')
    bands = policy['lead_time_bands']
    if not isinstance(bands, list) or not bands:
        raise ValueError('Finestre di prenotazione mancanti.')
    previous = -1
    for band in bands:
        end = band['through_days']
        if type(end) is not int or not previous < end <= 366:
            raise ValueError('Finestre di anticipo non ordinate.')
        previous = end
        low = finite(band['low_occupancy'], 0, 1)
        high = finite(band['high_occupancy'], 0, 1)
        if low >= high:
            raise ValueError('Soglie occupazione sovrapposte.')
        finite(band['low_multiplier'], .5, 1)
        finite(band['high_multiplier'], 1, 2)
    if previous != 366:
        raise ValueError('Le finestre devono coprire l’intero orizzonte.')
    for day, rate in policy.get('date_reference_prices', {}).items():
        date.fromisoformat(day)
        finite(rate, .01, 100000)


def policy_fingerprint(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def enrich_gap_context(account_id, property_id, target_date, context, policy):
    """Add only fresh, scoped booking velocity and confirmed gap evidence."""
    from pricepilot.services.operational_store import get_inventory_rows, get_reservation_metrics
    enriched = dict(context or {})
    try:
        metrics = get_reservation_metrics(
            account_id, property_id, target_date, target_date + timedelta(days=30)
        )
        stamp = datetime.fromisoformat(str(metrics.get('observed_at', '')).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        if metrics.get('complete') is True and 0 <= age <= 6 * 3600:
            enriched.update(metrics_complete=True,
                            pickup_7d_nights=metrics.get('pickup_7d_nights'),
                            metrics_observed_at=metrics.get('observed_at'))
    except (KeyError, TypeError, ValueError):
        pass
    gap_enabled = (policy.get('gap_rule') or {}).get('enabled') or (policy.get('minimum_stay_rule') or {}).get('enabled')
    if not gap_enabled:
        return enriched
    rows = get_inventory_rows(account_id, property_id, target_date-timedelta(days=8), target_date+timedelta(days=9))
    selected = {}
    try:
        for row in rows:
            stamp = datetime.fromisoformat(row['observed_at'].replace('Z', '+00:00'))
            if stamp.tzinfo is None or not 0 <= (datetime.now(timezone.utc)-stamp).total_seconds() <= 6*3600:
                return enriched
            day = date.fromisoformat(row['date'])
            if day in selected:
                return enriched
            selected[day] = row
        start = end = target_date
        if selected.get(target_date, {}).get('state') != 'open':
            return enriched
        while selected.get(start-timedelta(days=1), {}).get('state') == 'open':
            start -= timedelta(days=1)
        while selected.get(end+timedelta(days=1), {}).get('state') == 'open':
            end += timedelta(days=1)
        count = (end-start).days+1
        if count > 7 or any(selected.get(boundary, {}).get('state') != 'booked' for boundary in (start-timedelta(days=1), end+timedelta(days=1))):
            return enriched
        minimums = [selected[start+timedelta(days=i)]['min_stay'] for i in range(count)]
        if any(type(n) is not int or n < 1 for n in minimums):
            return enriched
        return {**enriched, 'gap_nights': count, 'gap_boundaries_confirmed': True, 'minimum_stay': max(minimums)}
    except (KeyError, TypeError, ValueError):
        return enriched


def calculate_calendar_price(*, current_price, occupancy, target_date, policy,
                             min_price, max_price, max_change_pct, today=None, inventory_context=None):
    validate_policy(policy)
    validate_occupancy(occupancy)
    today = today or pricing_today()
    days_until = (target_date - today).days
    if not 0 <= days_until <= 366:
        raise DataUnavailable('Data fuori dall’orizzonte calendario.')
    band = next(b for b in policy['lead_time_bands'] if days_until <= b['through_days'])
    reference = float(policy.get('date_reference_prices', {}).get(target_date.isoformat(), policy['reference_price']))
    multiplier = 1.0
    rule = 'occupazione nella fascia neutra'
    if occupancy < float(band['low_occupancy']):
        multiplier = float(band['low_multiplier'])
        rule = 'occupazione sotto la soglia impostata'
    elif occupancy > float(band['high_occupancy']):
        multiplier = float(band['high_multiplier'])
        rule = 'occupazione sopra la soglia impostata'
    weekend = target_date.weekday() in (4, 5)
    weekend_multiplier = float(policy.get('weekend_multiplier', 1)) if weekend else 1
    context = inventory_context or {}
    pacing = policy.get('pacing_rule') or {}
    pacing_multiplier = 1.0
    pacing_note = 'pacing non usato: dati completi e recenti non disponibili'
    pickup = context.get('pickup_7d_nights')
    if (pacing.get('enabled') and days_until <= pacing['through_days']
            and context.get('metrics_complete') is True and type(pickup) is int):
        if pickup <= pacing['low_pickup_7d_nights']:
            pacing_multiplier = float(pacing['low_multiplier'])
            pacing_note = f'pickup basso: {pickup} notti prenotate negli ultimi 7 giorni'
        elif pickup >= pacing['high_pickup_7d_nights']:
            pacing_multiplier = float(pacing['high_multiplier'])
            pacing_note = f'pickup alto: {pickup} notti prenotate negli ultimi 7 giorni'
        else:
            pacing_note = f'pickup neutro: {pickup} notti prenotate negli ultimi 7 giorni'
    gap = policy.get('gap_rule') or {}
    gap_multiplier = 1.0
    gap_note = ''
    manual_actions = []
    gap_nights = context.get('gap_nights')
    if gap.get('enabled') and days_until <= gap['through_days'] and type(gap_nights) is int and 1 <= gap_nights <= gap['max_nights']:
        if context.get('gap_boundaries_confirmed') and context.get('minimum_stay') is not None and context['minimum_stay'] <= gap_nights:
            gap_multiplier = float(gap['multiplier'])
            gap_note = f' | Vuoto di {gap_nights} notti tra prenotazioni confermate'
        else:
            gap_note = ' | Vuoto non scontato: verificare confini e soggiorno minimo'
    minimum_stay = policy.get('minimum_stay_rule') or {}
    if (minimum_stay.get('enabled') and days_until <= minimum_stay['through_days']
            and context.get('gap_boundaries_confirmed') and type(gap_nights) is int
            and gap_nights <= minimum_stay['max_gap_nights']
            and type(context.get('minimum_stay')) is int and context['minimum_stay'] > gap_nights):
        manual_actions.append({
            'type': 'minimum_stay_review',
            'current_minimum_stay': context['minimum_stay'],
            'suggested_minimum_stay': gap_nights,
            'reason': f'Vuoto di {gap_nights} notti tra prenotazioni confermate non prenotabile con soggiorno minimo {context["minimum_stay"]}.',
        })
    # Upward evidence wins over a discount. Discounts never stack: the most
    # conservative explicit reduction is applied once to the stable reference.
    upward = [m for m in (multiplier, pacing_multiplier) if m > 1]
    if upward:
        effective_multiplier = max(upward)
    elif gap_multiplier < 1:
        effective_multiplier = min(multiplier, pacing_multiplier, gap_multiplier)
    elif multiplier < 1:
        effective_multiplier = min(multiplier, pacing_multiplier)
    else:
        effective_multiplier = 1.0
    candidate = reference * effective_multiplier * weekend_multiplier
    # Only explicit owner constraints, no hidden market/weekend/demand floors.
    recommended, safety = apply_all_safety(
        old_price=current_price, new_price=candidate, min_price=min_price,
        max_price=max_price, max_change_pct=max_change_pct,
        break_even=float(policy.get('break_even', 0)), lead_time_limits=False)
    reason = (f'Data {target_date.isoformat()} | Calendario proprio: {occupancy:.0%} nella finestra di 30 giorni '
              f'dalla data analizzata | Anticipo {days_until} giorni | {rule} | '
              f'Riferimento EUR {reference:.2f} | Fattore occupazione {multiplier:.2f} | '
              f'{pacing_note} | Fattore pacing {pacing_multiplier:.2f} | '
              f'Fattore weekend impostato {weekend_multiplier:.2f}{gap_note}. '
              'Confronto competitor a cura del proprietario prima dell’approvazione.')
    return {'recommended_price': recommended, 'delta_vs_base': round((recommended/current_price-1)*100, 2),
            'delta_vs_market': None, 'confidence_score': 0.0,
            'confidence_kind': 'not_estimated_rules_only', 'is_weekend': weekend,
            'has_event': False, 'days_until': days_until, 'safety_note': safety,
            'reason': reason, 'breakdown': {'pricing_basis': 'calendar_only',
                'reference_price': reference, 'occupancy_multiplier': multiplier,
                'pacing_multiplier': pacing_multiplier, 'pickup_7d_nights': pickup,
                'weekend_multiplier': weekend_multiplier, 'lead_time_days': days_until,
                'gap_multiplier': gap_multiplier, 'gap_nights': gap_nights,
                'effective_multiplier': effective_multiplier,
                'unbounded_reference_target': round(candidate, 2),
                'policy_fingerprint': policy_fingerprint(policy), 'manual_actions': manual_actions,
                'adr': None, 'revpar': None, 'market_comparison': 'owner_manual_review'}}
