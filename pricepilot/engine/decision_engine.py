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
import logging
from datetime import date, datetime
from typing import Dict, Optional

from pricepilot.engine.pricing_engine import calculate_recommended_price
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


def process_decision(
    property_id: int = 1,
    occupancy: float = 0.65,
    target_date: date = None,
    event: str = "",
    season_factor: float = 1.0,
    event_factor: float = 1.0,
    competitor_count: int = 10,
    force_mode: Optional[str] = None,
    break_even: float = 0.0,
    data_source: str = "demo",
    occupancy_source: str = "demo",
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
        event:            Stringa evento.
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
    d    = target_date or date.today()
    prop = get_property(property_id)

    if not prop:
        prop = {
            "id": property_id, "name": "Default Property",
            "min_price": 50.0, "max_price": 500.0,
            "sync_mode": "advisory",
        }
        logger.warning(f"Property {property_id} not found, using defaults")

    account_id = int(prop.get("account_id") or 1)
    plan       = normalize_plan(get_effective_plan_for_property(prop))
    requested_mode = force_mode or effective_sync_mode(plan, prop.get("sync_mode", "advisory"))
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
            "event": event,
            "is_weekend": d.weekday() >= 5,
            "reason": "Prezzo bloccato manualmente dal proprietario.",
            "safety_note": "locked_manual",
            "guardrail_status": "locked",
            "guardrail_reasons": ["manual_price_lock"],
            "days_until": max(0, (d - date.today()).days) if d >= date.today() else 0,
            "data_source": data_source,
            "occupancy_source": occupancy_source,
            "calendar_status": "locked",
        }

    # Analisi mercato
    market_provider = get_market_data_provider()
    market_result = market_provider.analyze(
        property_id=property_id,
        target_date=d,
        event=event,
        competitor_count=competitor_count,
        persist=True,
        account_id=account_id,
        source=data_source,
    )
    stats = market_result.market_stats

    save_occupancy(
        property_id,
        d.isoformat(),
        occupancy,
        source=occupancy_source,
        account_id=account_id,
    )

    # days_until per guardrail
    days_until = max(0, (d - date.today()).days) if d >= date.today() else 0

    # Calcola prezzo
    has_event = bool(event and event.lower() not in ("none", "0", ""))
    pricing   = calculate_recommended_price(
        base_price       = base_price,
        market_avg       = stats["market_avg"],
        occupancy        = occupancy,
        target_date      = d,
        has_event        = has_event,
        min_price        = min_price,
        max_price        = max_price,
        competitor_count = competitor_count,
        season_factor    = season_factor,
        event_factor     = event_factor,
        days_until       = days_until,
        break_even       = break_even,
        competitor_avg   = stats["market_avg"],
        max_change_pct   = float(guardrails.get("max_change_pct", 0.20)),
    )

    recommended = pricing["recommended_price"]
    pct_change  = pricing["delta_vs_base"]
    confidence  = float(pricing.get("confidence_score", 0.0))

    sanity_ok, sanity_note = competitor_sanity_check(
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
    reason = _build_reason(
        old_price          = base_price,
        new_price          = recommended,
        occupancy          = occupancy,
        market_avg         = stats["market_avg"],
        event              = event,
        is_weekend         = pricing["is_weekend"],
        occ_high_threshold = 0.80,
        occ_low_threshold  = 0.30,
    )

    # ── Calcola campi arricchiti per decision_log ─────────────────────────────
    _comp_avg = stats.get("market_avg", 0)
    _mpi      = (round((recommended / _comp_avg) * 100, 1)
                 if _comp_avg > 0 else None)          # Market Price Index
    _factors  = json.dumps(
        pricing.get("breakdown", {}), ensure_ascii=False
    )                                                  # Breakdown fattori (JSON)
    _strategy = prop.get("strategy", CONFIG.get("strategy", "balanced"))

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
            f"plan={plan} | requested_mode={requested_mode} | event={event} | "
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
    decision_label, applied = _apply_mode(
        mode       = mode,
        old_price  = base_price,
        new_price  = recommended,
        prop       = prop,
        d          = d,
        event      = event,
        log_id     = log_id,
        occupancy  = occupancy,
        market_avg = stats["market_avg"],
        reason     = reason,
    )

    # Aggiorna decision_log con la label finale
    from pricepilot.core.database import get_conn
    with get_conn() as conn:
        conn.execute(
            "UPDATE decision_log SET decision=?, applied=? WHERE id=?",
            (decision_label, int(applied), log_id)
        )

    calendar_status = (
        "applied" if applied else
        "pending_approval" if mode == "approval" else
        "simulated" if mode == "auto" else
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
        f"market_avg=EUR{stats['market_avg']:.2f} | applied={applied} | {reason}"
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
        "market_stats":      stats,
        "competitors":       market_result.competitors,
        "breakdown":         pricing["breakdown"],
        "decision":          decision_label,
        "applied":           applied,
        "occupancy":         occupancy,
        "event":             event,
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
) -> tuple:
    """Applica la logica della modalita. Ritorna (decision_label, applied: bool)."""
    pct = (new_price - old_price) / max(old_price, 1) * 100

    if mode == "auto":
        cm_result = _channel_manager_update(prop, new_price, d)
        is_real    = bool(cm_result.get("ok") and cm_result.get("is_real"))
        cm_tag     = "[LIVE]" if is_real else "[SIMULATED]"
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
        tg_sent = _telegram_send_approval(
            prop, old_price, new_price, occupancy, market_avg, event, log_id, reason
        )
        if not tg_sent:
            notify_price_change(d.isoformat(), old_price, new_price, event)
        return decision, False

    else:  # advisory
        decision = f"ADVISORY: suggerisce {new_price:.2f} ({pct:+.1f}% vs base)"
        _telegram_send_recommendation(
            prop, old_price, new_price, occupancy, market_avg, event, reason
        )
        return decision, False


def _channel_manager_update(prop: Dict, new_price: float, d: date) -> Dict:
    """Chiama il ChannelManager per aggiornare il listing remoto."""
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
            "is_real":    result.is_real,
            "error":      result.error,
        }
    except Exception as exc:
        logger.error(f"_channel_manager_update error: {exc}")
        return {"ok": False, "platform": "unknown", "listing_id": "", "is_real": False, "error": str(exc)}


def _telegram_send_approval(
    prop: Dict, old_price: float, new_price: float,
    occupancy: float, market_avg: float, event: str,
    log_id: Optional[int], reason: str = "",
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
    """Invia un consiglio Free/advisory via Telegram senza pulsanti di approvazione."""
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
            f"Media mercato: EUR {market_avg:.2f}\n"
            f"Occupancy: {occupancy * 100:.0f}%"
            f"{event_line}\n\n"
            f"Motivo: {reason}\n\n"
            f"Piano Free: aggiorna manualmente il prezzo sulle tue OTA."
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
    """Approva una decisione senza dichiararla applicata se manca una sync reale."""
    from pricepilot.core.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, account_id, property_id, new_price, decision FROM decision_log WHERE id=?",
            (log_id,)
        ).fetchone()
        if not row:
            logger.warning(f"Decisione {log_id} non trovata.")
            return {
                "approved": False,
                "applied": False,
                "status": "not_found",
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
        if "[APPROVED" not in decision:
            decision = decision + " [APPROVED_PENDING_MANUAL_SYNC]"
        conn.execute(
            "UPDATE decision_log SET applied=0, decision=? WHERE id=?",
            (decision, log_id)
        )
        property_id = row["property_id"]
        new_price = float(row["new_price"])
    update_calendar_status_for_decision(
        decision_log_id=log_id,
        status="approved_pending_manual_sync",
        applied_price=None,
        notes=f"Approvato: prezzo {new_price:.2f} in attesa di sync manuale/OTA.",
    )
    record_audit_event(
        action="decision_approved",
        entity_type="decision_log",
        entity_id=log_id,
        account_id=row_account_id,
        property_id=property_id,
        source="telegram_or_api",
        status="approved_pending_manual_sync",
        details={"applied": False},
    )
    logger.info(f"Decisione {log_id} approvata, in attesa di aggiornamento manuale/listing.")
    return {
        "approved": True,
        "applied": False,
        "status": "approved_pending_manual_sync",
        "message": "Decisione approvata. Aggiorna manualmente il prezzo sul canale finche non colleghiamo un channel manager reale.",
    }
