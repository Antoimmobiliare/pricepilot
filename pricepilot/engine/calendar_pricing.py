"""Explicit owner rules, stable reference rates, no external market requests.

Thresholds/multipliers are configuration, not learned market demand or forecasts.
Using the same reference prevents repeated six-hour cycles compounding discounts.
"""
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pricepilot.core.config import BASE_DIR
from pricepilot.core.data_quality import DataUnavailable, validate_occupancy
from pricepilot.pricing.safety import apply_all_safety


DEFAULT_PRICING_TIMEZONE = 'Europe/Rome'
DEFAULT_CHECKIN_TIME = '15:00'

LEAD_TIME_STANDARD = 'STANDARD'
LEAD_TIME_WATCH = 'WATCH'
LEAD_TIME_LAST_MINUTE = 'LAST_MINUTE'
LEAD_TIME_URGENT = 'URGENT'
LEAD_TIME_SAME_DAY = 'SAME_DAY'

WATCH_MAX_HOURS = 72
LAST_MINUTE_MAX_HOURS = 48
URGENT_MAX_HOURS = 24

DEFAULT_UNSOLD_RISK = {
    'enabled': False,
    'urgency_weights': {
        LEAD_TIME_STANDARD: 0.0,
        LEAD_TIME_WATCH: 0.15,
        LEAD_TIME_LAST_MINUTE: 0.40,
        LEAD_TIME_URGENT: 0.70,
        LEAD_TIME_SAME_DAY: 1.0,
    },
    'max_amplification': 0.50,
    'max_total_discount': 0.15,
    'minimum_negative_signals': 2,
}


def pricing_today(timezone_name=None):
    """Calendar day used for lead time, stable across local and cloud workers."""
    name = str(timezone_name or os.getenv('PRICEPILOT_TIMEZONE', DEFAULT_PRICING_TIMEZONE)).strip()
    try:
        return datetime.now(ZoneInfo(name)).date()
    except ZoneInfoNotFoundError:
        raise DataUnavailable('Fuso orario pricing non valido.') from None


def _policy_timezone(policy):
    name = str(policy.get('timezone') or DEFAULT_PRICING_TIMEZONE).strip()
    try:
        return name, ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise ValueError('Fuso orario della policy non valido.') from None


def _policy_checkin_time(policy):
    raw = str(policy.get('checkin_time') or DEFAULT_CHECKIN_TIME).strip()
    try:
        parsed = time.fromisoformat(raw)
    except ValueError:
        raise ValueError('Orario di check-in della policy non valido.') from None
    if parsed.tzinfo is not None or parsed.second or parsed.microsecond:
        raise ValueError('Orario di check-in della policy non valido.')
    return raw, parsed


def calculate_lead_time(*, target_date, policy, now=None, today=None):
    """Return actual elapsed hours until local check-in and its urgency band."""
    timezone_name, local_zone = _policy_timezone(policy)
    checkin_time, local_checkin_time = _policy_checkin_time(policy)
    if now is not None and now.tzinfo is None:
        raise ValueError('Il datetime corrente deve includere il fuso orario.')
    if now is None:
        if today is not None:
            now = datetime.combine(today, time.min, tzinfo=local_zone)
        else:
            now = datetime.now(local_zone)
    current = now.astimezone(local_zone)
    checkin = datetime.combine(target_date, local_checkin_time, tzinfo=local_zone)
    elapsed = checkin.astimezone(timezone.utc) - current.astimezone(timezone.utc)
    hours = elapsed.total_seconds() / 3600
    days = (target_date - current.date()).days
    if hours <= 0:
        raise DataUnavailable('Check-in già trascorso: nessuna proposta ordinaria consentita.')
    if target_date == current.date():
        band = LEAD_TIME_SAME_DAY
    elif hours <= URGENT_MAX_HOURS:
        band = LEAD_TIME_URGENT
    elif hours <= LAST_MINUTE_MAX_HOURS:
        band = LEAD_TIME_LAST_MINUTE
    elif hours <= WATCH_MAX_HOURS:
        band = LEAD_TIME_WATCH
    else:
        band = LEAD_TIME_STANDARD
    return {
        'hours_until_checkin': hours,
        'days_until': days,
        'lead_time_band': band,
        'urgency_band': band,
        'checkin_datetime': checkin.isoformat(),
        'checkin_time': checkin_time,
        'timezone': timezone_name,
    }


def _format_lead_time(hours):
    total_minutes = max(0, int(round(float(hours) * 60)))
    whole_hours, minutes = divmod(total_minutes, 60)
    return f'{whole_hours}h {minutes:02d}m'


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


def _unsold_risk_settings(policy):
    configured = policy.get('unsold_risk')
    if configured is None:
        return dict(DEFAULT_UNSOLD_RISK)
    if not isinstance(configured, dict):
        raise ValueError('Configurazione rischio invenduto non valida.')
    settings = {**DEFAULT_UNSOLD_RISK, **configured}
    settings['urgency_weights'] = {
        **DEFAULT_UNSOLD_RISK['urgency_weights'],
        **(configured.get('urgency_weights') or {}),
    }
    return settings


def validate_policy(policy):
    _policy_timezone(policy)
    _policy_checkin_time(policy)
    finite(policy['reference_price'], .01, 100000)
    finite(policy.get('weekend_multiplier', 1), .5, 2)
    finite(policy.get('break_even', 0), 0, 100000)
    finite(policy.get('minimum_change_eur', 1), .01, 1000)
    risk = _unsold_risk_settings(policy)
    if type(risk.get('enabled')) is not bool:
        raise ValueError('Attivazione rischio invenduto non valida.')
    weights = risk.get('urgency_weights')
    if not isinstance(weights, dict):
        raise ValueError('Pesi urgenza non validi.')
    ordered_weights = []
    for name in (LEAD_TIME_STANDARD, LEAD_TIME_WATCH, LEAD_TIME_LAST_MINUTE,
                 LEAD_TIME_URGENT, LEAD_TIME_SAME_DAY):
        ordered_weights.append(finite(weights.get(name), 0, 1))
    if ordered_weights[0] != 0 or ordered_weights != sorted(ordered_weights):
        raise ValueError('I pesi urgenza devono partire da zero e crescere progressivamente.')
    finite(risk.get('max_amplification'), 0, 1)
    finite(risk.get('max_total_discount'), 0, .30)
    minimum_signals = risk.get('minimum_negative_signals')
    if type(minimum_signals) is not int or not 2 <= minimum_signals <= 3:
        raise ValueError('Numero minimo di segnali negativi non valido.')
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
                             min_price, max_price, max_change_pct, today=None, now=None,
                             inventory_context=None):
    validate_policy(policy)
    validate_occupancy(occupancy)
    lead_time = calculate_lead_time(target_date=target_date, policy=policy, now=now, today=today)
    days_until = lead_time['days_until']
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
    # Preserve the pre-unsold-risk rule: weak occupancy combined with strong
    # pickup is a base-pricing conflict and keeps the published price.  The
    # broader risk conflict below has a narrower purpose: it blocks only the
    # extra urgency amplification when any observed signals disagree.
    base_signal_conflict = multiplier < 1 and pacing_multiplier > 1
    signal_conflict = base_signal_conflict
    risk = _unsold_risk_settings(policy)
    risk_enabled = risk['enabled'] is True
    negative_signals = []
    positive_signals = []
    if multiplier < 1:
        negative_signals.append({'signal': 'occupancy_weak', 'multiplier': multiplier})
    elif multiplier > 1:
        positive_signals.append({'signal': 'occupancy_strong', 'multiplier': multiplier})
    if pacing_multiplier < 1:
        negative_signals.append({'signal': 'pickup_weak', 'multiplier': pacing_multiplier})
    elif pacing_multiplier > 1:
        positive_signals.append({'signal': 'pickup_strong', 'multiplier': pacing_multiplier})
    confirmed_gap = bool(
        gap_multiplier < 1 and context.get('gap_boundaries_confirmed') is True
        and type(gap_nights) is int
    )
    if confirmed_gap:
        negative_signals.append({'signal': 'confirmed_isolated_gap',
                                 'multiplier': gap_multiplier, 'gap_nights': gap_nights})
    if risk_enabled:
        signal_conflict = bool(negative_signals and positive_signals)
    if signal_conflict:
        manual_actions.append({
            'type': 'pricing_signal_conflict',
            'occupancy': occupancy,
            'pickup_7d_nights': pickup,
            'reason': (
                'Segnali propri positivi e negativi in conflitto: non amplificare '
                'la pressione last-minute e verificare calendario, pickup e durata '
                'delle nuove prenotazioni.'
            ),
        })
    # Upward evidence wins over a discount. Discounts never stack: first find
    # one base multiplier from explicit owner rules, then optionally strengthen
    # that same reduction using coherent, observed evidence.
    upward = [m for m in (multiplier, pacing_multiplier) if m > 1]
    if base_signal_conflict:
        effective_multiplier = 1.0
    elif upward:
        effective_multiplier = max(upward)
    elif gap_multiplier < 1:
        effective_multiplier = min(multiplier, pacing_multiplier, gap_multiplier)
    elif multiplier < 1:
        effective_multiplier = min(multiplier, pacing_multiplier)
    else:
        effective_multiplier = 1.0
    base_effective_multiplier = effective_multiplier
    urgency_weight = float(risk['urgency_weights'][lead_time['lead_time_band']])
    unsold_risk_pressure = 0.0
    urgency_action = 'disabled_legacy'
    if risk_enabled:
        urgency_action = 'standard_rules_only' if urgency_weight == 0 else 'hold_no_negative_signals'
        if negative_signals:
            strongest_reduction = max(1-float(item['multiplier']) for item in negative_signals)
            evidence_coherence = min(1.0, len(negative_signals) / 3.0)
            max_total_discount = float(risk['max_total_discount'])
            severity = min(1.0, strongest_reduction / max_total_discount) if max_total_discount else 0.0
            unsold_risk_pressure = round(urgency_weight * evidence_coherence * severity, 4)
            if signal_conflict:
                urgency_action = 'hold_signal_conflict'
            elif len(negative_signals) < risk['minimum_negative_signals']:
                urgency_action = 'hold_insufficient_negative_evidence'
            elif urgency_weight > 0 and strongest_reduction > 0:
                extra_reduction = (strongest_reduction * float(risk['max_amplification'])
                                   * urgency_weight * evidence_coherence)
                total_reduction = min(max_total_discount, strongest_reduction + extra_reduction)
                effective_multiplier = min(effective_multiplier, 1-total_reduction)
                urgency_action = 'amplify_negative_signals'
        if positive_signals and not negative_signals:
            urgency_action = 'hold_positive_signals'
    rules_confidence = ('conflicted' if signal_conflict else
                        'supported' if len(negative_signals) >= risk['minimum_negative_signals'] else
                        'limited')
    # A risk-level conflict blocks only the urgency amplification.  The legacy
    # occupancy-low/pickup-high conflict still keeps the published price;
    # otherwise an independently justified base rule remains effective.
    candidate = (current_price if base_signal_conflict else
                 reference * effective_multiplier * weekend_multiplier)
    # Only explicit owner constraints, no hidden market/weekend/demand floors.
    recommended, safety = apply_all_safety(
        old_price=current_price, new_price=candidate, min_price=min_price,
        max_price=max_price, max_change_pct=max_change_pct,
        break_even=float(policy.get('break_even', 0)), lead_time_limits=False)
    if base_signal_conflict:
        urgency_note = ('Segnali positivi e negativi in conflitto; prezzo invariato '
                        'e revisione prudente')
    elif signal_conflict:
        urgency_note = ('Segnali positivi e negativi in conflitto; nessuna pressione '
                        'last-minute aggiuntiva e revisione prudente')
    elif urgency_action == 'amplify_negative_signals':
        urgency_note = ('I segnali negativi coerenti aumentano il rischio di invenduto; '
                        'pressione vendita applicata entro i guardrail')
    elif urgency_action == 'hold_positive_signals':
        urgency_note = 'I segnali positivi non giustificano pressione vendita aggiuntiva'
    elif urgency_action == 'hold_insufficient_negative_evidence':
        urgency_note = 'Segnali negativi insufficienti: nessuna pressione aggiuntiva dovuta all’urgenza'
    elif urgency_action == 'standard_rules_only':
        urgency_note = 'Fascia standard: nessuna pressione last-minute'
    elif urgency_action == 'hold_no_negative_signals':
        urgency_note = 'Nessun segnale negativo affidabile: prezzo protetto'
    else:
        urgency_note = 'Modello rischio invenduto non configurato: applicate le regole esistenti'
    reason = (f'Data {target_date.isoformat()} | Calendario proprio: {occupancy:.0%} nella finestra di 30 giorni '
              f'dalla data analizzata | Check-in tra {_format_lead_time(lead_time["hours_until_checkin"])} '
              f'({lead_time["lead_time_band"]}) | Anticipo {days_until} giorni | {rule} | '
              f'Riferimento EUR {reference:.2f} | Fattore occupazione {multiplier:.2f} | '
              f'{pacing_note} | Fattore pacing {pacing_multiplier:.2f} | '
              f'Fattore weekend impostato {weekend_multiplier:.2f}{gap_note}'
              f' | {urgency_note}.')
    return {'recommended_price': recommended, 'delta_vs_base': round((recommended/current_price-1)*100, 2),
            'delta_vs_market': None, 'confidence_score': 0.0,
            'confidence_kind': 'not_estimated_rules_only', 'is_weekend': weekend,
            'has_event': False, 'days_until': days_until,
            'hours_until_checkin': lead_time['hours_until_checkin'],
            'lead_time_band': lead_time['lead_time_band'],
            'urgency_band': lead_time['urgency_band'],
            'checkin_datetime': lead_time['checkin_datetime'],
            'timezone': lead_time['timezone'],
            'safety_note': safety,
            'reason': reason, 'breakdown': {'pricing_basis': 'calendar_only',
                'reference_price': reference, 'occupancy_multiplier': multiplier,
                'pacing_multiplier': pacing_multiplier, 'pickup_7d_nights': pickup,
                'weekend_multiplier': weekend_multiplier, 'lead_time_days': days_until,
                'hours_until_checkin': lead_time['hours_until_checkin'],
                'lead_time_band': lead_time['lead_time_band'],
                'urgency_band': lead_time['urgency_band'],
                'checkin_datetime': lead_time['checkin_datetime'],
                'checkin_time': lead_time['checkin_time'],
                'timezone': lead_time['timezone'],
                'gap_multiplier': gap_multiplier, 'gap_nights': gap_nights,
                'base_signal_conflict': base_signal_conflict,
                'signal_conflict': signal_conflict,
                'negative_signals': negative_signals,
                'positive_signals': positive_signals,
                'rules_confidence': rules_confidence,
                'unsold_risk_pressure': unsold_risk_pressure,
                'urgency_weight': urgency_weight,
                'urgency_action': urgency_action,
                'base_effective_multiplier': base_effective_multiplier,
                'effective_multiplier': effective_multiplier,
                'unbounded_reference_target': round(candidate, 2),
                'policy_fingerprint': policy_fingerprint(policy), 'manual_actions': manual_actions,
                'adr': None, 'revpar': None, 'market_comparison': 'owner_manual_review'}}
