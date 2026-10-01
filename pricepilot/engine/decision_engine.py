"""
PricePilot - Decision Engine
Gestisce le tre modalita di applicazione del prezzo:

  advisory  -> suggerisce solo, non applica nulla
  approval  -> notifica e aspetta conferma prima di applicare
  auto      -> applica automaticamente il prezzo raccomandato

Il Decision Engine e il punto di orchestrazione centrale:
  1. Riceve proprieta + dati di mercato
  2. Invoca pricing_engine per calcolare il prezzo
  3. Genera il MOTIVO del cambiamento (reason)
  4. In base alla sync_mode decide come procedere
  5. Salva la decisione in decision_log
"""
import json
import os
import logging
import functools
import inspect
from datetime import date, datetime, timezone
from typing import Dict, Optional

from pricepilot.engine.pricing_engine import calculate_recommended_price, OCC_HIGH_THRESHOLD, OCC_LOW_THRESHOLD
from pricepilot.core.data_quality import DataUnavailable, demo_enabled, calendar_pricing_enabled, validate_market, validate_occupancy
from pricepilot.engine.calendar_pricing import (calculate_calendar_price, load_policy,
    policy_fingerprint, enrich_gap_context, pricing_today)
from pricepilot.providers.contracts import MarketDataResult
from pricepilot.core.config import CONFIG
from pricepilot.core.plans import effective_sync_mode, normalize_plan
from pricepilot.core.database import (
    get_property, save_decision_log, save_occupancy,
    get_telegram_link_by_property, get_effective_plan_for_property,
    get_guardrail_policy, count_auto_actions_today, record_audit_event,
    get_notification_preferences, record_notification_log,
    get_current_price_for_date, save_price_recommendation,
    update_calendar_status_for_decision,
)
from pricepilot.pricing.safety import competitor_sanity_check
from pricepilot.notifications.notifier import notify_price_change
from pricepilot.providers.registry import (
    get_channel_manager_provider,
    get_market_data_provider,
)

logger = logging.getLogger("pricepilot.decision_engine")

SYNC_MODES = {
    "advisory": "Solo suggerimento - nessuna azione automatica",
    "approval": "Richiede conferma prima di applicare il prezzo",
    "auto":     "Applica automaticamente il prezzo raccomandato",
}


def _reuse_proposal(row, old_price, new_price, factors, mode):
    """A declined identical proposal stays declined; uncertain sends need reconciliation."""
    state = str(row.get('decision') or '')
    if '[APPLYING]' in state or '[APPROVED_SYNC_FAILED]' in state:
        return True
    try:
        previous = json.loads(row.get('factors') or '{}')
        context_keys = ('policy_fingerprint', 'current_price_source', 'reference_price',
                        'occupancy_multiplier', 'pacing_multiplier', 'pickup_7d_nights',
                        'weekend_multiplier', 'lead_time_days', 'gap_multiplier',
                        'gap_nights', 'effective_multiplier', 'manual_actions')
        previous_context = {key: previous.get(key) for key in context_keys}
        current_context = {key: factors.get(key) for key in context_keys}
        same = (abs(float(row['old_price'])-old_price) < .005
                and abs(float(row['new_price'])-new_price) < .005
                and row.get('mode') == mode
                and previous_context == current_context)
        if not same:
            return False
        if '[REJECTED]' in state or state.startswith(('UNCHANGED', 'ADVISORY')) or '[APPROVED_PENDING_MANUAL_SYNC]' in state:
            return True
        stamp = datetime.fromisoformat(str(row['timestamp']).replace('Z', '+00:00'))
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
        return state.startswith('PENDING_APPROVAL') and 0 <= (datetime.now(timezone.utc)-stamp).total_seconds() < 6*3600
    except (ValueError, TypeError, KeyError):
        return False


def _scoped_property(property_id, account_id=None):
    return get_property(property_id, account_id=account_id)


def _build_reason(
    old_price: float,
    new_price: float,
    occupancy: float,
    market_avg: float,
    event: str,
    is_weekend: bool,
    occ_high_threshold: float = 0.80,
    occ_low_threshold: float = 0.30,
) -> str:
    """
    Costruisce una stringa human-readable che spiega il motivo del cambiamento.
    Usata nei messaggi Telegram e nel log.
    """
    reasons = []
    pct = (new_price - old_price) / max(old_price, 1) * 100

    # Occupancy
    if occupancy > occ_high_threshold:
        reasons.append(f"Alta occupancy ({occupancy*100:.0f}%)")
    elif occupancy < occ_low_threshold:
        reasons.append(f"Bassa occupancy ({occupancy*100:.0f}%)")

    # Evento
    if event and event.lower() not in ("none", "0", ""):
        reasons.append(f"Evento: {event}")

    # Mercato
    if market_avg > 0:
        if market_avg > old_price * 1.05:
            reasons.append(f"Mercato in rialzo (media {market_avg:.0f}EUR)")
        elif market_avg < old_price * 0.95:
            reasons.append(f"Mercato in ribasso (media {market_avg:.0f}EUR)")
        else:
            reasons.append(f"In linea con il mercato (media {market_avg:.0f}EUR)")

    # Weekend
    if is_weekend:
        reasons.append("Weekend (domanda alta)")

    # Direzione cambiamento
    if pct > 0:
        reasons.append(f"Aumento {pct:+.1f}%")
    elif pct < 0:
        reasons.append(f"Riduzione {pct:.1f}%")

    return " | ".join(reasons) if reasons else "Ottimizzazione automatica"


def _process_decision(
    property_id: int = 1,
    occupancy: float = 0.65,
    target_date: date = None,
    event: str = "",
    event_label: str = "",
    season_factor: float = 1.0,
    event_factor: float = 1.0,
    competitor_count: int = 10,
    force_mode: Optional[str] = None,
    break_even: float = 0.0,
    data_source: str = "demo",
    occupancy_source: str = "demo",
    defer_notifications: bool = False,
    account_id: Optional[int] = None,
) -> Dict:
    """
    Entry point principale del Decision Engine.

    1. Carica la proprieta dal DB
    2. Esegue analisi di mercato
    3. Calcola il prezzo raccomandato
    4. Costruisce il motivo del cambiamento
    5. Applica la logica della modalita (advisory/approval/auto)
    6. Salva la decisione nel decision_log
    7. Ritorna il risultato completo

    Args:
        property_id:      ID proprieta nel DB.
        occupancy:        Tasso occupancy [0.0-1.0].
        target_date:      Data target.
        event:            Categoria evento normalizzata.
        event_label:      Nome leggibile dell'evento da mostrare all'utente.
        season_factor:    Moltiplicatore stagionale [0.5-2.0].
        event_factor:     Moltiplicatore evento [1.0-2.0].
        competitor_count: Numero competitor da analizzare.
        force_mode:       Override della sync_mode della proprieta.
        break_even:       Prezzo minimo operativo (0 = disabilitato).
        data_source:      Origine dati mercato/eventi (demo/manual/api).
        occupancy_source: Origine occupancy (demo/manual/pms).

    Returns:
        Dict con tutti i dettagli della decisione.
    """
    d    = target_date or pricing_today()
    own_calendar = calendar_pricing_enabled()
    prop = _scoped_property(property_id, account_id)

    if not prop:
        raise ValueError("Proprieta non trovata: nessuna configurazione inventata.")
    inventory_context = {}
    if not demo_enabled():
        from pricepilot.providers.registry import get_occupancy_provider
        observation = get_occupancy_provider().estimate(property_id=property_id, target_date=d,
                                                        account_id=int(prop.get("account_id") or 1))
        if observation.raw.get("target_state") != "open":
            raise DataUnavailable("Data non vendibile o stato inventario sconosciuto.")
        occupancy, occupancy_source = observation.occupancy, observation.source
        inventory_context = observation.raw
    validate_occupancy(occupancy)

    account_id = int(prop.get("account_id") or 1)
    plan       = normalize_plan(get_effective_plan_for_property(prop))
    requested_mode = effective_sync_mode(plan, force_mode or prop.get("sync_mode", "advisory"))
    if (own_calendar or not demo_enabled()) and requested_mode == "auto":
        requested_mode = "approval"
    min_price  = float(prop.get("min_price", 50))
    max_price  = float(prop.get("max_price", 500))
    base_price, current_price_source = get_current_price_for_date(prop, d.isoformat())
    guardrails = get_guardrail_policy(account_id=account_id, property_id=property_id)

    if current_price_source == "manual_lock":
        decision_label = f"LOCKED_MANUAL: prezzo bloccato a {base_price:.2f}"
        log_id = save_decision_log({
            "account_id": account_id,
            "property_id": property_id,
            "old_price": base_price,
            "new_price": base_price,
            "market_avg": None,
            "occupancy": occupancy,
            "decision": decision_label,
            "mode": requested_mode,
            "applied": 0,
            "notes": "Prezzo bloccato manualmente dal calendario. PricePilot non modifica questa data.",
            "date": d.isoformat(),
            "competitor_avg": None,
            "strategy": prop.get("strategy", CONFIG.get("strategy", "balanced")),
            "factors": "{}",
            "mpi": None,
            "current_price_source": current_price_source,
            "data_source": data_source,
        })
        record_audit_event(
            action="decision_skipped_locked_price",
            entity_type="decision_log",
            entity_id=log_id,
            account_id=account_id,
            property_id=property_id,
            source="decision_engine",
            status="locked",
            details={"date": d.isoformat(), "locked_price": base_price},
        )
        logger.info(
            "Property %s | %s | prezzo bloccato manualmente a EUR%.2f",
            property_id, d.isoformat(), base_price,
        )
        return {
            "log_id": log_id,
            "property_id": property_id,
            "property_name": prop.get("name", ""),
            "mode": requested_mode,
            "requested_mode": requested_mode,
            "plan": plan,
            "date": d.isoformat(),
            "old_price": base_price,
            "current_price_source": current_price_source,
            "recommended_price": base_price,
            "delta_vs_market": 0.0,
            "delta_vs_base": 0.0,
            "confidence_score": 1.0,
            "market_stats": {},
            "competitors": [],
            "breakdown": {},
            "decision": decision_label,
            "applied": False,
            "occupancy": occupancy,
            "event": event_label or event,
            "event_type": event,
            "is_weekend": d.weekday() >= 5,
            "reason": "Prezzo bloccato manualmente dal proprietario.",
            "safety_note": "locked_manual",
            "guardrail_status": "locked",
            "guardrail_reasons": ["manual_price_lock"],
            "days_until": max(0, (d - pricing_today()).days) if d >= pricing_today() else 0,
            "data_source": data_source,
            "occupancy_source": occupancy_source,
            "calendar_status": "locked",
        }

    # Analisi mercato
    market_result = MarketDataResult(competitors=[], market_stats={"market_avg": None, "competitor_count": 0}, source="calendar_only") if own_calendar else get_market_data_provider().analyze(
        property_id=property_id,
        target_date=d,
        event=event,
        competitor_count=competitor_count,
        persist=True,
        account_id=account_id,
        source=data_source,
    )
    stats = market_result.market_stats
    if not demo_enabled():
        if not own_calendar:
            validate_market(market_result)
        if occupancy_source in {"demo", "demo_occupancy", "manual_default_occupancy"}:
            raise DataUnavailable("Occupazione simulata esclusa dal percorso operativo.")
        if current_price_source == "price_range_midpoint":
            raise DataUnavailable("Prezzo corrente non acquisito: il punto medio dei limiti non e una tariffa osservata.")

    save_occupancy(
        property_id,
        d.isoformat(),
        occupancy,
        source=occupancy_source,
        account_id=account_id,
    )

    # days_until per guardrail
    today = pricing_today()
    days_until = max(0, (d - today).days) if d >= today else 0

    # Calcola prezzo
    has_event = bool(event and event.lower() not in ("none", "0", ""))
    if own_calendar:
        policy = load_policy(account_id, property_id)
        inventory_context = enrich_gap_context(account_id, property_id, d, inventory_context, policy)
        pricing = calculate_calendar_price(current_price=base_price, occupancy=occupancy,
            target_date=d, policy=policy, min_price=min_price, max_price=max_price,
            max_change_pct=float(guardrails.get("max_change_pct", 0.20)), inventory_context=inventory_context)
        has_event, event, event_label = False, "", ""
    else:
        pricing = calculate_recommended_price(
        base_price       = base_price,
        market_avg       = stats["market_avg"],
        occupancy        = occupancy,
        target_date      = d,
        has_event        = has_event,
        min_price        = min_price,
        max_price        = max_price,
        competitor_count = len(market_result.competitors),
        season_factor    = season_factor,
        event_factor     = event_factor,
        days_until       = days_until,
        break_even       = break_even,
        competitor_avg   = stats["market_avg"],
        max_change_pct   = float(guardrails.get("max_change_pct", 0.20)),
        strategy_name    = prop.get("strategy") or CONFIG.get("strategy", "balanced"),
    )

    pricing.setdefault('breakdown', {})['current_price_source'] = current_price_source
    recommended = pricing["recommended_price"]
    if own_calendar and abs(recommended-base_price) < float(policy.get('minimum_change_eur', 1)):
        recommended = base_price
        pricing['recommended_price'] = base_price
        pricing['delta_vs_base'] = 0.0
    pct_change  = pricing["delta_vs_base"]
    confidence  = float(pricing.get("confidence_score", 0.0))

    sanity_ok, sanity_note = (True, "not_applicable_calendar_only") if own_calendar else competitor_sanity_check(
        old_price=base_price,
        competitor_avg=stats["market_avg"],
        max_deviation=float(guardrails.get("competitor_outlier_pct", 0.60)),
    )

    guardrail_reasons = []
    mode = requested_mode
    if not sanity_ok:
        guardrail_reasons.append(sanity_note)
    if mode == "auto" and not int(guardrails.get("auto_enabled", 1)):
        guardrail_reasons.append("auto_disabled_by_policy")
    if mode == "auto" and (
        market_result.source == "competitor_provider_unconfigured"
        or int(stats.get("competitor_count") or 0) <= 0
    ):
        guardrail_reasons.append("market_data_provider_not_configured")
    if mode == "auto" and confidence < float(guardrails.get("min_confidence_auto", 0.80)):
        guardrail_reasons.append(
            f"confidence_below_auto_threshold ({confidence:.2f} < {float(guardrails.get('min_confidence_auto', 0.80)):.2f})"
        )
    if mode == "auto" and abs(pct_change) / 100 >= float(guardrails.get("require_approval_pct", 0.15)):
        guardrail_reasons.append(
            f"large_change_requires_approval ({pct_change:+.1f}%)"
        )
    if mode == "auto":
        daily_auto = count_auto_actions_today(property_id, d.isoformat())
        if daily_auto >= int(guardrails.get("max_daily_auto_changes", 4)):
            guardrail_reasons.append(
                f"daily_auto_limit_reached ({daily_auto}/{int(guardrails.get('max_daily_auto_changes', 4))})"
            )

    if mode == "auto" and guardrail_reasons:
        mode = "approval"

    # Costruisce motivo del cambiamento
    reason = pricing["reason"] if own_calendar else _build_reason(
        old_price          = base_price,
        new_price          = recommended,
        occupancy          = occupancy,
        market_avg         = stats["market_avg"],
        event              = event_label or event,
        is_weekend         = pricing["is_weekend"],
        occ_high_threshold = OCC_HIGH_THRESHOLD,
        occ_low_threshold  = OCC_LOW_THRESHOLD,
    )

    # ── Calcola campi arricchiti per decision_log ─────────────────────────────
    _comp_avg = stats.get("market_avg", 0)
    _mpi      = (round((recommended / _comp_avg) * 100, 1)
                 if _comp_avg is not None and _comp_avg > 0 else None)          # Market Price Index
    _factors  = json.dumps(
        pricing.get("breakdown", {}), ensure_ascii=False
    )                                                  # Breakdown fattori (JSON)
    _strategy = "calendar_rules" if own_calendar else prop.get("strategy", CONFIG.get("strategy", "balanced"))

    if own_calendar:
        from pricepilot.core.database import get_calendar_price, get_decision_log_entry
        existing_calendar = get_calendar_price(property_id, d.isoformat(), account_id)
        existing = get_decision_log_entry(existing_calendar['decision_log_id'], account_id) if existing_calendar and existing_calendar.get('decision_log_id') else None
        if existing and _reuse_proposal(existing, base_price, recommended, pricing['breakdown'], mode):
            return {'log_id': existing['id'], 'property_id': property_id, 'property_name': prop.get('name', ''),
                    'date': d.isoformat(), 'old_price': base_price, 'recommended_price': recommended,
                    'mode': mode, 'decision': existing['decision'], 'applied': bool(existing.get('applied')),
                    'calendar_status': existing_calendar.get('status'), 'deduplicated': True,
                    'reason': reason, 'occupancy': occupancy, 'breakdown': pricing['breakdown']}

    # Salva in decision_log (prima, per avere log_id per Telegram)
    log_entry = {
        "account_id":    account_id,
        "property_id":   property_id,
        "old_price":     base_price,
        "new_price":     recommended,
        "market_avg":    stats["market_avg"],
        "occupancy":     occupancy,
        "decision":      "PENDING",
        "mode":          mode,
        "applied":       0,
        "notes":         (
            f"plan={plan} | requested_mode={requested_mode} | event_type={event} | "
            f"event={event_label or event} | "
            f"conf={pricing['confidence_score']} | guardrails="
            f"{'; '.join(guardrail_reasons) if guardrail_reasons else 'ok'} | {reason}"
        ),
        # ── Nuovi campi data storage ───────────────────────────────────────
        "date":          d.isoformat(),        # data del pricing (YYYY-MM-DD)
        "competitor_avg": _comp_avg,            # media grezza competitor
        "strategy":      _strategy,             # strategia pricing attiva
        "factors":       _factors,             # breakdown JSON
        "mpi":           _mpi,                 # Market Price Index
        "current_price_source": current_price_source,
        "data_source":   market_result.source or data_source,
    }
    log_id = save_decision_log(log_entry)
    record_audit_event(
        action="decision_created",
        entity_type="decision_log",
        entity_id=log_id,
        account_id=account_id,
        property_id=property_id,
        source="decision_engine",
        status="guarded" if guardrail_reasons else "ok",
        details={
            "plan": plan,
            "requested_mode": requested_mode,
            "mode": mode,
            "guardrails": guardrail_reasons,
            "old_price": base_price,
            "new_price": recommended,
            "confidence_score": confidence,
            "current_price_source": current_price_source,
            "data_source": market_result.source or data_source,
        },
    )

    # Applica modalita
    manual_actions = pricing.get('breakdown', {}).get('manual_actions') or []
    if own_calendar and abs(recommended-base_price) < .005 and manual_actions:
        action_type = str(manual_actions[0].get('type') or '')
        review_reason = (
            "verificare il soggiorno minimo"
            if action_type == "minimum_stay_review"
            else "verificare i segnali calendario in conflitto"
        )
        decision_label, applied = (f"MANUAL_REVIEW: prezzo invariato; {review_reason}", False)
    elif own_calendar and abs(recommended-base_price) < .005:
        decision_label, applied = ("UNCHANGED: tariffa gia allineata alle regole calendario", False)
    else:
        decision_label, applied = _apply_mode(
        mode       = mode,
        old_price  = base_price,
        new_price  = recommended,
        prop       = prop,
        d          = d,
        event      = event_label or event,
        log_id     = log_id,
        occupancy  = occupancy,
        market_avg = stats["market_avg"],
        reason     = reason,
        notify     = False,
        )

    # Aggiorna decision_log tramite il repository attivo (SQLite o Supabase).
    from pricepilot.core.database import update_decision_state
    update_decision_state(
        log_id,
        account_id=account_id,
        applied=bool(applied),
        decision=decision_label,
    )

    calendar_status = (
        "manual_review" if own_calendar and abs(recommended-base_price) < .005 and manual_actions else
        "unchanged" if own_calendar and abs(recommended-base_price) < .005 else
        "applied" if applied else
        "pending_approval" if mode == "approval" else
        ("simulated" if demo_enabled() else "sync_failed") if mode == "auto" else
        "recommended"
    )
    save_price_recommendation(
        account_id=account_id,
        property_id=property_id,
        date_str=d.isoformat(),
        current_price=base_price,
        recommended_price=recommended,
        status=calendar_status,
        decision_log_id=log_id,
        notes=decision_label,
        current_price_source=current_price_source,
    )
    # Publish only after both decision state and calendar pointer are committed.
    if not defer_notifications and calendar_status != 'unchanged':
        if calendar_status == 'manual_review':
            _telegram_send_recommendation(prop, base_price, recommended, occupancy, stats['market_avg'], event, reason)
        elif mode == 'approval':
            _telegram_send_approval(prop, base_price, recommended, occupancy, stats['market_avg'], event, log_id, reason, d)
        elif mode == 'advisory':
            _telegram_send_recommendation(prop, base_price, recommended, occupancy, stats['market_avg'], event, reason)
    record_audit_event(
        action="decision_mode_processed",
        entity_type="decision_log",
        entity_id=log_id,
        account_id=account_id,
        property_id=property_id,
        source="decision_engine",
        status="applied" if applied else "not_applied",
        details={"mode": mode, "decision": decision_label, "calendar_status": calendar_status},
    )

    logger.info(
        f"[{mode.upper()}] Property {property_id} | "
        f"EUR{base_price:.2f} -> EUR{recommended:.2f} ({pct_change:+.1f}%) | "
        f"pricing_source={market_result.source} | applied={applied} | {reason}"
    )

    return {
        "log_id":            log_id,
        "property_id":       property_id,
        "property_name":     prop.get("name", ""),
        "mode":              mode,
        "requested_mode":    requested_mode,
        "plan":              plan,
        "date":              d.isoformat(),
        "old_price":         base_price,
        "current_price_source": current_price_source,
        "recommended_price": recommended,
        "delta_vs_market":   pricing["delta_vs_market"],
        "delta_vs_base":     pct_change,
        "confidence_score":  confidence,
        "confidence_kind":   pricing.get("confidence_kind", "heuristic"),
        "market_stats":      stats,
        "competitors":       market_result.competitors,
        "breakdown":         pricing["breakdown"],
        "decision":          decision_label,
        "applied":           applied,
        "occupancy":         occupancy,
        "event":             event_label or event,
        "event_type":        event,
        "is_weekend":        pricing["is_weekend"],
        "reason":            reason,
        "safety_note":       pricing.get("safety_note", "ok"),
        "guardrail_status":  "review_required" if guardrail_reasons else "ok",
        "guardrail_reasons": guardrail_reasons,
        "days_until":        days_until,
        "data_source":       market_result.source or data_source,
        "occupancy_source":  occupancy_source,
        "calendar_status":   calendar_status,
    }


@functools.wraps(_process_decision)
def process_decision(*args, **kwargs):
    """Serialize proposal creation per tenant/property/date across workers."""
    from pricepilot.core.operation_lock import pricing_date_lease
    cycle_deadline = kwargs.pop("_cycle_deadline", None)
    bound = inspect.signature(_process_decision).bind(*args, **kwargs)
    bound.apply_defaults()
    values = bound.arguments
    if values['account_id'] is None and not demo_enabled():
        raise ValueError('account_id obbligatorio per il pricing operativo.')
    prop = _scoped_property(values['property_id'], values['account_id'])
    if not prop:
        raise ValueError('Proprieta non disponibile per questo account.')
    with pricing_date_lease(int(prop.get('account_id') or 1), int(prop['id']), (values['target_date'] or pricing_today()).isoformat(), deadline=cycle_deadline):
        return _process_decision(*args, **kwargs)


def _apply_mode(
    mode: str,
    old_price: float,
    new_price: float,
    prop: Dict,
    d: date,
    event: str,
    log_id: Optional[int] = None,
    occupancy: float = 0.65,
    market_avg: float = 0.0,
    reason: str = "",
    notify: bool = True,
) -> tuple:
    """Applica la logica della modalita. Ritorna (decision_label, applied: bool)."""
    pct = (new_price - old_price) / max(old_price, 1) * 100

    if mode == "auto":
        cm_result = _channel_manager_update(prop, new_price, d)
        is_real    = bool(cm_result.get("ok") and cm_result.get("is_real"))
        cm_tag     = "[CHANNEL_CONFIRMED]" if is_real else ("[SIMULATED]" if demo_enabled() else "[NOT_APPLIED]")
        action     = "AUTO_APPLIED" if is_real else "AUTO_RECOMMENDED"
        decision   = (
            f"{action} {cm_tag}: {old_price:.2f}->{new_price:.2f} ({pct:+.1f}%) "
            f"| {cm_result.get('platform','?')}/{cm_result.get('listing_id','?')}"
        )
        if not cm_result.get("ok"):
            decision += f" | update_failed={cm_result.get('error', 'unknown')}"

        logger.info(f"[AUTO] Prezzo {'applicato' if is_real else 'simulato'}: EUR{new_price:.2f} {cm_tag}")
        if is_real:
            _telegram_notify_auto(prop, old_price, new_price, event, reason)
            if abs(pct) > 5:
                notify_price_change(d.isoformat(), old_price, new_price, event)
        return decision, is_real

    elif mode == "approval":
        decision = f"PENDING_APPROVAL: {old_price:.2f}->{new_price:.2f} ({pct:+.1f}%)"
        logger.info(f"[APPROVAL] In attesa conferma per EUR{new_price:.2f}")
        tg_sent = not notify or _telegram_send_approval(
            prop, old_price, new_price, occupancy, market_avg, event, log_id, reason
        )
        if not tg_sent:
            notify_price_change(d.isoformat(), old_price, new_price, event)
        return decision, False

    else:  # advisory
        decision = f"ADVISORY: suggerisce {new_price:.2f} ({pct:+.1f}% vs base)"
        if notify:
            _telegram_send_recommendation(
            prop, old_price, new_price, occupancy, market_avg, event, reason
        )
        return decision, False


def _channel_manager_update(prop: Dict, new_price: float, d: date) -> Dict:
    """Chiama il ChannelManager per aggiornare il listing remoto."""
    if not demo_enabled() and os.getenv("PRICEPILOT_ALLOW_CHANNEL_WRITES") != "1":
        return {"ok": False, "is_real": False, "platform": "disabled", "listing_id": "",
                "attempted": False,
                "error": "Invio prezzi disabilitato fino al collaudo dei collegamenti."}
    try:
        result = get_channel_manager_provider().update_price(
            prop=prop,
            new_price=new_price,
            target_date=d,
        )
        return {
            "ok":         result.ok,
            "platform":   result.platform,
            "listing_id": result.listing_id,
            "new_price":  new_price,
            "is_real":    result.is_real,
            "attempted":  True,
            "error":      result.error,
            "raw":        result.raw,
        }
    except Exception as exc:
        logger.error(f"_channel_manager_update error: {exc}")
        return {"ok": False, "platform": "unknown", "listing_id": "", "is_real": False,
                "attempted": True, "error": str(exc)}


def _telegram_send_approval(
    prop: Dict, old_price: float, new_price: float,
    occupancy: float, market_avg: float, event: str,
    log_id: Optional[int], reason: str = "", target_date: Optional[date] = None,
) -> bool:
    """Tenta di inviare la richiesta di approvazione via Telegram con motivo."""
    try:
        from pricepilot.services.telegram_bot import is_configured, send_approval_request
        if not is_configured():
            return False

        prop_id = prop.get("id")
        link    = get_telegram_link_by_property(prop_id) if prop_id else None
        prefs   = get_notification_preferences(int(prop.get("account_id") or 1), prop_id)
        if not int(prefs.get("telegram_enabled", 1)) or not int(prefs.get("approval_alerts", 1)):
            record_notification_log(
                event_type="approval_request",
                status="skipped_preferences",
                account_id=int(prop.get("account_id") or 1),
                property_id=prop_id,
                payload={"log_id": log_id},
            )
            return False
        if not link or not link.get("chat_id"):
            logger.info(
                f"[APPROVAL] Nessun chat_id Telegram per property_id={prop_id}. "
                "Collega prima il bot dalla dashboard."
            )
            record_notification_log(
                event_type="approval_request",
                status="skipped_no_chat",
                account_id=int(prop.get("account_id") or 1),
                property_id=prop_id,
                payload={"log_id": log_id},
            )
            return False

        result = send_approval_request(
            log_id     = log_id or 0,
            prop_name  = prop.get("name", "Proprieta"),
            old_price  = old_price,
            new_price  = new_price,
            occupancy  = occupancy,
            market_avg = market_avg,
            event      = event,
            chat_id    = link["chat_id"],
            reason     = reason,
            target_date = target_date.isoformat() if target_date else "",
        )
        if result.get("ok"):
            logger.info(f"[APPROVAL] Telegram inviato a chat_id={link['chat_id']}")
            record_notification_log(
                event_type="approval_request",
                status="sent",
                account_id=int(prop.get("account_id") or 1),
                property_id=prop_id,
                recipient=str(link["chat_id"]),
                message_id=str((result.get("result") or {}).get("message_id", "")),
                payload={"log_id": log_id, "new_price": new_price},
            )
            return True
        else:
            logger.warning(f"[APPROVAL] Telegram fallito: {result.get('error')}")
            record_notification_log(
                event_type="approval_request",
                status="failed",
                account_id=int(prop.get("account_id") or 1),
                property_id=prop_id,
                recipient=str(link["chat_id"]),
                error=str(result.get("error", "")),
                payload={"log_id": log_id, "new_price": new_price},
            )
            return False

    except Exception as exc:
        logger.error(f"_telegram_send_approval error: {exc}")
        return False


def _telegram_send_recommendation(
    prop: Dict, old_price: float, new_price: float,
    occupancy: float, market_avg: float, event: str, reason: str = "",
) -> bool:
    """Invia un consiglio advisory via Telegram senza pulsanti di approvazione."""
    try:
        from pricepilot.services.telegram_bot import is_configured, send_message
        if not is_configured():
            return False

        prop_id = prop.get("id")
        link    = get_telegram_link_by_property(prop_id) if prop_id else None
        prefs   = get_notification_preferences(int(prop.get("account_id") or 1), prop_id)
        if not int(prefs.get("telegram_enabled", 1)):
            record_notification_log(
                event_type="recommendation",
                status="skipped_preferences",
                account_id=int(prop.get("account_id") or 1),
                property_id=prop_id,
            )
            return False
        if not link or not link.get("chat_id"):
            record_notification_log(
                event_type="recommendation",
                status="skipped_no_chat",
                account_id=int(prop.get("account_id") or 1),
                property_id=prop_id,
            )
            return False

        pct   = (new_price - old_price) / max(old_price, 1) * 100
        arrow = "su" if pct > 0 else ("giu" if pct < 0 else "stabile")
        event_line = f"\nEvento: {event}" if event and event not in ("none", "", "0") else ""
        text = (
            f"*PricePilot - Consiglio prezzo*\n"
            f"Proprieta: {prop.get('name', 'Proprieta')}\n"
            f"Prezzo attuale: EUR {old_price:.2f}\n"
            f"Prezzo suggerito: EUR {new_price:.2f} ({pct:+.1f}%, {arrow})\n"
            + (f"Media mercato: EUR {market_avg:.2f}\n" if market_avg is not None else "Analisi del tuo calendario; confronto competitor manuale.\n")
            +
            f"Occupancy: {occupancy * 100:.0f}%"
            f"{event_line}\n\n"
            f"Motivo: {reason}\n\n"
            f"Modalita consiglio: aggiorna manualmente il prezzo sulle tue OTA."
        )
        result = send_message(link["chat_id"], text)
        record_notification_log(
            event_type="recommendation",
            status="sent" if result.get("ok") else "failed",
            account_id=int(prop.get("account_id") or 1),
            property_id=prop_id,
            recipient=str(link["chat_id"]),
            message_id=str((result.get("result") or {}).get("message_id", "")),
            error=str(result.get("error", "")),
            payload={"new_price": new_price},
        )
        return bool(result.get("ok"))
    except Exception as exc:
        logger.error(f"_telegram_send_recommendation error: {exc}")
        return False


def _telegram_notify_auto(
    prop: Dict, old_price: float, new_price: float,
    event: str, reason: str = "",
) -> None:
    """Invia notifica (senza pulsanti) per auto-apply."""
    try:
        from pricepilot.services.telegram_bot import is_configured, notify_auto_applied
        if not is_configured():
            return

        prop_id = prop.get("id")
        link    = get_telegram_link_by_property(prop_id) if prop_id else None
        prefs   = get_notification_preferences(int(prop.get("account_id") or 1), prop_id)
        if not int(prefs.get("telegram_enabled", 1)) or not int(prefs.get("auto_reports", 1)):
            record_notification_log(
                event_type="auto_report",
                status="skipped_preferences",
                account_id=int(prop.get("account_id") or 1),
                property_id=prop_id,
            )
            return
        if not link or not link.get("chat_id"):
            record_notification_log(
                event_type="auto_report",
                status="skipped_no_chat",
                account_id=int(prop.get("account_id") or 1),
                property_id=prop_id,
            )
            return

        result = notify_auto_applied(
            chat_id   = link["chat_id"],
            prop_name = prop.get("name", "Proprieta"),
            old_price = old_price,
            new_price = new_price,
            event     = event,
            reason    = reason,
        )
        record_notification_log(
            event_type="auto_report",
            status="sent" if result.get("ok") else "failed",
            account_id=int(prop.get("account_id") or 1),
            property_id=prop_id,
            recipient=str(link["chat_id"]),
            message_id=str((result.get("result") or {}).get("message_id", "")),
            error=str(result.get("error", "")),
            payload={"new_price": new_price},
        )
    except Exception as exc:
        logger.error(f"_telegram_notify_auto error: {exc}")


def approve_decision(log_id: int, account_id: Optional[int] = None) -> Dict:
    """Approva una decisione e applica il prezzo se esiste una sync reale."""
    from pricepilot.core.database import get_decision_log_entry, update_decision_state, claim_decision_application, get_calendar_price
    row = get_decision_log_entry(log_id, account_id=account_id)
    if not row:
        logger.warning(f"Decisione {log_id} non trovata.")
        return {
            "approved": False,
            "applied": False,
            "status": "forbidden" if account_id is not None else "not_found",
            "message": "Decisione non trovata.",
        }

    row_account_id = int(row["account_id"] or 1)
    if account_id is not None and row_account_id != int(account_id):
        logger.warning(f"Decisione {log_id} non appartiene all'account {account_id}.")
        return {
            "approved": False,
            "applied": False,
            "status": "forbidden",
            "message": "Decisione non disponibile per questo account.",
        }

    decision = row["decision"] or ""
    if row.get("applied"):
        return {"approved": True, "applied": True, "status": "already_applied", "message": "Prezzo gia applicato: nessun nuovo invio."}
    if (not decision.startswith("PENDING_APPROVAL") or
            any(tag in decision for tag in ("[APPLYING]", "[REJECTED]", "[APPROVED_"))):
        return {"approved": False, "applied": False, "status": "not_pending", "message": "Decisione non in attesa o esito da riconciliare."}
    property_id = row["property_id"]
    new_price = float(row["new_price"])
    date_str = row["date"] or pricing_today().isoformat()

    prop = _scoped_property(int(property_id), row_account_id) if property_id else None
    if prop and int(prop.get("account_id") or 1) == row_account_id:
        try:
            target_date = datetime.fromisoformat(str(date_str)).date()
        except ValueError:
            return {"approved": False, "applied": False, "status": "invalid_date"}
        current, source = get_current_price_for_date(prop, target_date.isoformat())
        calendar = get_calendar_price(int(property_id), target_date.isoformat(), row_account_id)
        if source == "manual_lock" or abs(current - float(row["old_price"])) > 0.005 or (
                calendar and calendar.get("decision_log_id") not in (None, log_id)):
            return {"approved": False, "applied": False, "status": "stale", "message": "Prezzo, lock o raccomandazione cambiati: ricalcolare."}
        if not demo_enabled():
            try:
                stamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
                stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
                age = (datetime.now(timezone.utc) - stamp).total_seconds()
                if target_date < pricing_today() or not 0 <= age <= 6 * 3600:
                    raise ValueError()
                from pricepilot.providers.registry import get_occupancy_provider
                inventory = get_occupancy_provider().estimate(property_id=int(property_id), target_date=target_date, account_id=row_account_id)
                if inventory.raw.get("target_state") != "open":
                    raise ValueError()
                if calendar_pricing_enabled():
                    factors = json.loads(row.get("factors") or "{}")
                    policy = load_policy(row_account_id, int(property_id))
                    if factors.get("pricing_basis") != "calendar_only" or factors.get("policy_fingerprint") != policy_fingerprint(policy):
                        raise ValueError()
                    if factors.get('current_price_source') != source:
                        raise ValueError()
                    guardrails = get_guardrail_policy(account_id=row_account_id, property_id=int(property_id))
                    refreshed_context = enrich_gap_context(row_account_id, int(property_id), target_date, inventory.raw, policy)
                    refreshed = calculate_calendar_price(current_price=current, occupancy=inventory.occupancy,
                        target_date=target_date, policy=policy, min_price=float(prop['min_price']),
                        max_price=float(prop['max_price']), max_change_pct=float(guardrails.get('max_change_pct', .2)), inventory_context=refreshed_context)
                    if abs(refreshed['recommended_price']-new_price) > .005:
                        raise ValueError()
                else:
                    validate_market(get_market_data_provider().analyze(property_id=int(property_id), target_date=target_date, account_id=row_account_id, persist=False))
            except (ValueError, KeyError, TypeError):
                return {"approved": False, "applied": False, "status": "expired_or_missing_data", "message": "Proposta scaduta o dati/regole cambiati: ricalcolare prima di approvare."}
        if not float(prop["min_price"]) <= new_price <= float(prop["max_price"]):
            return {"approved": False, "applied": False, "status": "limits_changed"}
        if not claim_decision_application(log_id, row_account_id, decision):
            return {"approved": False, "applied": False, "status": "already_claimed"}
        cm_result = _channel_manager_update(prop, new_price, target_date)
    else:
        cm_result = {
            "ok": False,
            "platform": "unknown",
            "listing_id": "",
            "is_real": False,
            "error": "Proprieta non trovata.",
        }

    is_real = bool(cm_result.get("ok") and cm_result.get("is_real"))
    sync_failed = bool(cm_result.get("attempted") and not cm_result.get("ok"))

    if is_real:
        tag = " [APPROVED_SYNCED]"
        status = "applied"
        applied = True
        applied_price = new_price
        notes = (
            f"Approvato e sincronizzato su {cm_result.get('platform')}/"
            f"{cm_result.get('listing_id')}: {new_price:.2f}."
        )
        message = "Decisione approvata e prezzo sincronizzato sul channel manager."
    elif sync_failed:
        tag = " [APPROVED_SYNC_FAILED]"
        status = "approved_sync_failed"
        applied = False
        applied_price = None
        notes = f"Approvato, ma la sync OTA e fallita: {cm_result.get('error', 'errore sconosciuto')}."
        message = "Decisione approvata, ma la sincronizzazione OTA e fallita. Controlla integrazione e log."
    else:
        tag = " [APPROVED_PENDING_MANUAL_SYNC]"
        status = "approved_pending_manual_sync"
        applied = False
        applied_price = None
        notes = f"Approvato: prezzo {new_price:.2f} in attesa di sync manuale/OTA."
        message = "Decisione approvata. Aggiorna manualmente il prezzo sul canale finche non colleghiamo un channel manager reale."

    if tag not in decision:
        decision = decision + tag
    update_decision_state(
        log_id,
        account_id=row_account_id,
        applied=bool(applied),
        decision=decision,
    )

    update_calendar_status_for_decision(
        decision_log_id=log_id,
        status=status,
        applied_price=applied_price,
        notes=notes,
    )
    record_audit_event(
        action="decision_approved",
        entity_type="decision_log",
        entity_id=log_id,
        account_id=row_account_id,
        property_id=property_id,
        source="telegram_or_api",
        status=status,
        details={"applied": applied, "channel_manager": cm_result},
    )
    logger.info(
        "Decisione %s approvata: status=%s, applied=%s, channel=%s/%s",
        log_id,
        status,
        applied,
        cm_result.get("platform"),
        cm_result.get("listing_id"),
    )
    return {
        "approved": True,
        "applied": applied,
        "status": status,
        "message": message,
        "channel_manager": cm_result,
    }
