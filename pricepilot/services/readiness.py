"""
PricePilot - Account readiness checks.

Small product-level checklist used before enabling channel manager/API work.
"""
from __future__ import annotations

from datetime import date
from typing import Dict, List

from pricepilot.core.database import (
    get_account,
    get_guardrail_policy,
    init_db,
    get_notification_preferences,
    get_properties,
    get_telegram_link_by_property,
    get_current_price_for_date,
)
from pricepilot.core.plans import get_plan


def account_readiness(account_id: int = 1) -> Dict:
    init_db()
    account = get_account(account_id) or {"id": account_id, "plan": "free"}
    plan = get_plan(account.get("plan"))
    properties = [p for p in get_properties() if int(p.get("account_id") or 1) == account_id]
    guardrails = get_guardrail_policy(account_id)
    notifications = get_notification_preferences(account_id)

    checks: List[Dict] = []
    checks.append(_check("account", "Account configurato", True, "Profilo di lavoro pronto."))
    checks.append(_check("billing", "Stato abbonamento", bool(account.get("billing_status")), _billing_detail(account.get("billing_status"))))
    checks.append(_check("properties", "Almeno una proprieta", bool(properties), f"{len(properties)} proprieta configurate."))
    checks.append(_check("plan", "Piano attivo", account.get("plan") in ("free", "plus", "pro"), plan["label"]))
    checks.append(_check("guardrails", "Regole sicurezza prezzi", bool(guardrails), "Limiti di variazione e controlli automatici presenti."))
    checks.append(_check("notifications", "Notifiche configurate", bool(notifications), "Preferenze Telegram e report presenti."))

    for prop in properties:
        prefix = f"property:{prop['id']}"
        checks.append(_check(prefix + ":name", f"{prop['name']} - dati base", bool(prop.get("city")), "Citta/zona impostata." if prop.get("city") else "Aggiungi citta/zona."))
        checks.append(_check(prefix + ":price_bounds", f"{prop['name']} - limiti prezzo", float(prop.get("min_price", 0)) < float(prop.get("max_price", 0)), "Min/max validi."))
        _, price_source = get_current_price_for_date(prop, date.today().isoformat())
        checks.append(_check(
            prefix + ":current_price",
            f"{prop['name']} - prezzo attuale",
            price_source != "price_range_midpoint",
            "Prezzo corrente impostato." if price_source != "price_range_midpoint" else "Imposta il prezzo attuale dalla sezione Prezzi attuali.",
        ))
        tg = get_telegram_link_by_property(int(prop["id"]))
        checks.append(_check(prefix + ":telegram", f"{prop['name']} - Telegram", bool(tg and tg.get("chat_id")), "Collegato." if tg and tg.get("chat_id") else "Collega Telegram per notifiche reali."))
        checks.append(_check(prefix + ":ota", f"{prop['name']} - OTA", bool(prop.get("listing_url") or prop.get("listing_id")), "Listing identificato." if prop.get("listing_url") or prop.get("listing_id") else "Aggiungi URL o listing id."))

    required = [c for c in checks if c["required"]]
    passed = [c for c in required if c["ok"]]
    score = round(len(passed) / len(required) * 100, 1) if required else 100.0

    blockers = [c for c in checks if c["required"] and not c["ok"]]
    warnings = [c for c in checks if not c["required"] and not c["ok"]]
    return {
        "account_id": account_id,
        "plan": plan,
        "score": score,
        "ready": score >= 80 and not blockers,
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
    }


def _check(key: str, label: str, ok: bool, detail: str = "", required: bool = True) -> Dict:
    return {
        "key": key,
        "label": label,
        "ok": bool(ok),
        "detail": detail,
        "required": required,
    }


def _billing_detail(status: str | None) -> str:
    labels = {
        "dev": "Demo locale: nessun pagamento reale collegato.",
        "trialing": "Prova gratuita attiva.",
        "active": "Abbonamento attivo.",
        "past_due": "Pagamento da verificare.",
        "canceled": "Abbonamento disattivato.",
    }
    return labels.get((status or "").lower(), status or "Non configurato.")
