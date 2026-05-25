"""
PricePilot - Scheduler
Esecuzione periodica del motore di pricing.
"""
import time
import logging
from datetime import date, datetime, timedelta
from typing import Callable, Dict, Optional

logger = logging.getLogger("pricepilot.scheduler")


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
        get_occupancy_provider,
    )

    d = target_date or date.today()
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
    properties = [p for p in get_properties() if int(p.get("account_id") or 1) == account_id]
    results = []
    errors = []
    property_results = []
    event_provider = get_event_provider()
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
                "event": getattr(event_provider, "name", type(event_provider).__name__),
                "occupancy": getattr(occupancy_provider, "name", type(occupancy_provider).__name__),
            },
        },
    )

    try:
        event = event_provider.event_to_string(event_provider.event_for_date(d))
        for prop in properties:
            try:
                occupancy = occupancy_provider.estimate(
                    property_id=int(prop["id"]),
                    target_date=d,
                    account_id=account_id,
                )
                result = process_decision(
                    property_id=int(prop["id"]),
                    occupancy=occupancy.occupancy,
                    target_date=d,
                    event=event,
                    competitor_count=int(plan.get("competitor_limit", 10)),
                    data_source="demo",
                    occupancy_source=occupancy.source,
                )
                results.append(result)
                property_results.append({
                    "property_id": prop.get("id"),
                    "property_name": prop.get("name", ""),
                    "status": "ok",
                    "mode": result.get("mode"),
                    "decision": result.get("decision"),
                    "recommended_price": result.get("recommended_price"),
                    "calendar_status": result.get("calendar_status"),
                })
            except Exception as exc:
                logger.error("Errore ciclo property_id=%s: %s", prop.get("id"), exc, exc_info=True)
                err = {
                    "property_id": prop.get("id"),
                    "property_name": prop.get("name", ""),
                    "error": str(exc),
                }
                errors.append(err)
                property_results.append({
                    "property_id": prop.get("id"),
                    "property_name": prop.get("name", ""),
                    "status": "error",
                    "error": str(exc),
                })

        status = "success" if not errors else ("partial_error" if results else "error")
        summary = {
            "date": d.isoformat(),
            "properties": len(properties),
            "decisions": len(results),
            "errors": errors,
            "property_results": property_results,
            "modes": {mode: sum(1 for r in results if r.get("mode") == mode)
                      for mode in ("advisory", "approval", "auto")},
            "providers": {
                "billing": getattr(billing_provider, "name", type(billing_provider).__name__),
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_periodic(lambda: run_pricing_cycle(source="scheduler_cli"), hours=6)
