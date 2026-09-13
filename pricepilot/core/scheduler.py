"""
PricePilot - Scheduler
Esecuzione periodica del motore di pricing.
"""
import os
import time
import logging
from datetime import date, datetime, timedelta
from typing import Callable, Dict, Optional
from pricepilot.core.data_quality import calendar_pricing_enabled
from pricepilot.engine.calendar_pricing import pricing_today
from types import SimpleNamespace

logger = logging.getLogger("pricepilot.scheduler")


def _event_context_for_property(event_provider, prop: dict, target_date: date, account_id: int) -> tuple[str, str]:
    """Resolve a normalized event type and a human label for one property."""
    event = None
    try:
        property_lookup = getattr(event_provider, "event_for_property", None)
        if callable(property_lookup):
            event = property_lookup(
                prop=prop,
                target_date=target_date,
                account_id=account_id,
            )
        else:
            event = event_provider.event_for_date(target_date)
    except Exception as exc:
        # A public-events API outage must not prevent the full pricing cycle.
        logger.warning(
            "Impossibile leggere eventi per property_id=%s: %s",
            prop.get("id"),
            exc,
        )
        try:
            event = event_provider.event_for_date(target_date)
        except Exception:
            event = None

    event_type = event_provider.event_to_string(event)
    label_lookup = getattr(event_provider, "event_label", None)
    if callable(label_lookup):
        event_label = str(label_lookup(event) or "").strip()
    elif event:
        event_label = str(event.get("name") or event_type).strip()
    else:
        event_label = ""
    return event_type, event_label


def run_periodic(func: Callable, hours: float = 6, once: bool = False) -> None:
    """
    Esegue func ogni `hours` ore. Se once=True esegue una volta sola.
    """
    if once:
        logger.info("Esecuzione singola...")
        func()
        return

    interval = hours * 3600
    logger.info(f"Avvio scheduler: ciclo ogni {hours}h")
    try:
        while True:
            logger.info("Ciclo pricing in esecuzione...")
            try:
                func()
            except Exception as e:
                logger.error(f"Errore nel ciclo: {e}", exc_info=True)
            logger.info(f"Prossimo ciclo tra {hours}h")
            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info("Scheduler fermato.")


def run_pricing_cycle(
    account_id: int = 1,
    target_date: Optional[date] = None,
    interval_hours: float = 6,
    source: str = "scheduler",
    horizon_days: Optional[int] = None,
) -> Dict:
    """
    Esegue un ciclo SaaS completo su tutte le proprieta dell'account.

    Questo e il punto da collegare poi a un worker/cron reale: oggi analizza,
    decide secondo piano e guardrail, registra run e audit.
    """
    from pricepilot.core.database import (
        finish_operation_run,
        get_properties,
        record_audit_event,
        try_start_operation_run,
    )
    from pricepilot.core.plans import get_plan
    from pricepilot.engine.decision_engine import process_decision
    from pricepilot.providers.registry import (
        get_billing_provider,
        get_event_provider,
        get_market_data_provider,
        get_occupancy_provider,
    )

    start_date = target_date or pricing_today()
    horizon = horizon_days if horizon_days is not None else (1 if target_date else int(os.getenv("PRICEPILOT_HORIZON_DAYS", "90")))
    if isinstance(horizon, bool) or not isinstance(horizon, int) or not 1 <= horizon <= 366:
        raise ValueError("Orizzonte pricing: intero fra 1 e 366 giorni.")
    d = start_date
    billing_provider = get_billing_provider()
    billing_plan = billing_provider.get_account_plan(account_id=account_id)
    plan = get_plan(billing_plan.plan)
    effective_interval = float(plan.get("analysis_interval_hours") or interval_hours)
    next_run_at = (datetime.utcnow() + timedelta(hours=effective_interval)).isoformat()
    stale_after_minutes = max(30, int(effective_interval * 60 * 2))
    run_id, active_run = try_start_operation_run(
        account_id=account_id,
        source=source,
        next_run_at=next_run_at,
        stale_after_minutes=stale_after_minutes,
    )
    if active_run:
        record_audit_event(
            action="pricing_cycle_skipped",
            entity_type="operation_run",
            entity_id=active_run.get("id"),
            account_id=account_id,
            source=source,
            status="skipped_running",
            details={
                "reason": "existing_cycle_running",
                "active_run_id": active_run.get("id"),
                "active_started_at": active_run.get("started_at"),
            },
        )
        return {
            "run": active_run,
            "results": [],
            "errors": [],
            "skipped": True,
            "message": "Ciclo gia in esecuzione per questo account.",
        }
    if run_id is None:
        raise RuntimeError("Impossibile avviare il ciclo pricing.")
    properties = get_properties(account_id=account_id)
    results = []
    errors = []
    property_results = []
    event_provider = None if calendar_pricing_enabled() else get_event_provider()
    market_provider = SimpleNamespace(name="not_used_calendar_only") if calendar_pricing_enabled() else get_market_data_provider()
    occupancy_provider = get_occupancy_provider()

    record_audit_event(
        action="pricing_cycle_started",
        entity_type="operation_run",
        entity_id=run_id,
        account_id=account_id,
        source=source,
        status="running",
        details={
            "property_count": len(properties),
            "date": d.isoformat(),
            "providers": {
                "billing": getattr(billing_provider, "name", type(billing_provider).__name__),
                "market": getattr(market_provider, "name", type(market_provider).__name__),
                "event": getattr(event_provider, "name", type(event_provider).__name__),
                "occupancy": getattr(occupancy_provider, "name", type(occupancy_provider).__name__),
            },
        },
    )

    try:
        for prop in properties:
            if os.getenv("PRICEPILOT_CHANNEL_PROVIDER") == "beds24" and os.getenv("PRICEPILOT_OCCUPANCY_PROVIDER") == "observed_inventory":
                try:
                    from pricepilot.services.beds24_sync import sync_property
                    sync_property(account_id, int(prop["id"]), start_date, horizon)
                except Exception as exc:
                    errors.append({"property_id": prop["id"], "date": start_date.isoformat(), "error": str(exc), "stage": "inventory_sync"})
                    continue
            for offset in range(horizon):
                d = start_date + timedelta(days=offset)
                try:
                    event_type, event_label = ("", "") if calendar_pricing_enabled() else _event_context_for_property(
                        event_provider,
                        prop,
                        d,
                        account_id,
                    )
                    occupancy = occupancy_provider.estimate(
                        property_id=int(prop["id"]),
                        target_date=d,
                        account_id=account_id,
                    )
                    if occupancy.raw.get("target_state") in {"booked", "owner_blocked", "maintenance_blocked", "unavailable"}:
                        property_results.append({"property_id": prop["id"], "date": d.isoformat(), "status": "skipped_unavailable"})
                        continue
                    result = process_decision(
                        property_id=int(prop["id"]),
                        occupancy=occupancy.occupancy,
                        target_date=d,
                        event=event_type,
                        event_label=event_label,
                        competitor_count=int(plan.get("competitor_limit", 10)),
                        data_source=getattr(market_provider, "name", "market_provider"),
                        occupancy_source=occupancy.source,
                        defer_notifications=True,
                        account_id=account_id,
                    )
                    results.append(result)
                    property_results.append({
                        "date": d.isoformat(),
                        "property_id": prop.get("id"),
                        "property_name": prop.get("name", ""),
                        "status": "ok",
                        "mode": result.get("mode"),
                        "decision": result.get("decision"),
                        "recommended_price": result.get("recommended_price"),
                        "calendar_status": result.get("calendar_status"),
                        "event": result.get("event", ""),
                        "event_type": result.get("event_type", "none"),
                    })
                except Exception as exc:
                    logger.error("Errore ciclo property_id=%s: %s", prop.get("id"), exc, exc_info=True)
                    err = {
                        "date": d.isoformat(),
                        "property_id": prop.get("id"),
                        "property_name": prop.get("name", ""),
                        "error": str(exc),
                    }
                    errors.append(err)
                    property_results.append({
                        "date": d.isoformat(),
                        "property_id": prop.get("id"),
                        "property_name": prop.get("name", ""),
                        "status": "error",
                        "error": str(exc),
                    })
        from pricepilot.services.telegram_bot import send_cycle_digest
        notifications = send_cycle_digest(account_id, results)
        status = "success" if not errors else ("partial_error" if results else "error")
        summary = {
            "date": start_date.isoformat(),
            "horizon_days": horizon,
            "end_exclusive": (start_date + timedelta(days=horizon)).isoformat(),
            "properties": len(properties),
            "notifications": notifications,
            "decisions": len(results),
            "errors": errors,
            "property_results": property_results,
            "modes": {mode: sum(1 for r in results if r.get("mode") == mode)
                      for mode in ("advisory", "approval", "auto")},
            "providers": {
                "billing": getattr(billing_provider, "name", type(billing_provider).__name__),
                "market": getattr(market_provider, "name", type(market_provider).__name__),
                "event": getattr(event_provider, "name", type(event_provider).__name__),
                "occupancy": getattr(occupancy_provider, "name", type(occupancy_provider).__name__),
            },
        }
        run = finish_operation_run(
            run_id=run_id,
            status=status,
            decisions_count=len(results),
            summary=summary,
            error="; ".join(e["error"] for e in errors[:3]),
            next_run_at=next_run_at,
        )
        record_audit_event(
            action="pricing_cycle_finished",
            entity_type="operation_run",
            entity_id=run_id,
            account_id=account_id,
            source=source,
            status=status,
            details=summary,
        )
        return {"run": run, "results": results, "errors": errors}
    except Exception as exc:
        run = finish_operation_run(
            run_id=run_id,
            status="error",
            decisions_count=len(results),
            summary={"date": d.isoformat(), "decisions": len(results), "errors": errors},
            error=str(exc),
            next_run_at=next_run_at,
        )
        record_audit_event(
            action="pricing_cycle_failed",
            entity_type="operation_run",
            entity_id=run_id,
            account_id=account_id,
            source=source,
            status="error",
            details={"error": str(exc)},
        )
        raise


def run_cloud_pricing_cycle(
    target_date: Optional[date] = None,
    interval_hours: float = 6,
    source: str = "cloud_scheduler",
    horizon_days: Optional[int] = None,
) -> Dict:
    """Esegue il ciclo su ogni account che possiede almeno una proprietà.

    Il job cloud chiama questa funzione una sola volta. Il blocco contro i
    doppioni resta per-account in ``run_pricing_cycle``: un errore su un
    tenant non interrompe gli altri e non può far partire due run paralleli
    sullo stesso portfolio.
    """
    from pricepilot.core.database import get_properties

    account_ids = sorted({
        int(prop.get("account_id") or 1)
        for prop in get_properties()
    })
    results = []
    errors = []

    for account_id in account_ids:
        try:
            result = run_pricing_cycle(
                account_id=account_id,
                target_date=target_date,
                interval_hours=interval_hours,
                source=source,
                horizon_days=horizon_days,
            )
            if result.get("errors"):
                errors.append({"account_id": account_id, "error": "Ciclo incompleto", "details": result["errors"]})
            results.append({
                "account_id": account_id,
                "status": "skipped" if result.get("skipped") else ("error" if result.get("errors") else "ok"),
                "run": result.get("run"),
                "decisions": len(result.get("results") or []),
                "errors": result.get("errors") or [],
            })
        except Exception as exc:
            logger.error(
                "Errore cloud scheduler account_id=%s: %s",
                account_id,
                exc,
                exc_info=True,
            )
            errors.append({"account_id": account_id, "error": str(exc)})

    return {
        "ok": not errors,
        "source": source,
        "accounts_processed": len(results),
        "accounts_skipped": sum(1 for item in results if item["status"] == "skipped"),
        "accounts_failed": len(errors),
        "results": results,
        "errors": errors,
    }


if __name__ == "__main__":
    os.environ.setdefault("PRICEPILOT_RUNTIME", "scheduler")
    logging.basicConfig(level=logging.INFO)
    run_periodic(lambda: run_pricing_cycle(source="scheduler_cli"), hours=6)
