"""
PricePilot - Subscription plan rules.

The current product stores the plan on each property. When a real billing/account
model exists, these rules can move to account-level entitlements.
"""
from __future__ import annotations

from typing import Dict


PLAN_FREE = "free"
PLAN_PLUS = "plus"
PLAN_PRO = "pro"

PLANS = (PLAN_FREE, PLAN_PLUS, PLAN_PRO)

PLAN_DEFINITIONS: Dict[str, Dict] = {
    PLAN_FREE: {
        "label": "Free",
        "sync_mode": "advisory",
        "max_properties": 1,
        "analysis_interval_hours": 6,
        "competitor_limit": 5,
        "telegram_recommendations": True,
        "telegram_approval": False,
        "ota_sync": False,
        "auto_apply": False,
        "features": {
            "pricing_cycle": True,
            "telegram_recommendations": True,
            "telegram_approval": False,
            "ota_sync": False,
            "auto_apply": False,
            "audit_log": True,
            "advanced_guardrails": False,
        },
        "description": "Consigli motivati via Telegram, aggiornamento manuale sulle OTA.",
    },
    PLAN_PLUS: {
        "label": "Plus",
        "sync_mode": "approval",
        "max_properties": 5,
        "analysis_interval_hours": 6,
        "competitor_limit": 10,
        "telegram_recommendations": True,
        "telegram_approval": True,
        "ota_sync": True,
        "auto_apply": False,
        "features": {
            "pricing_cycle": True,
            "telegram_recommendations": True,
            "telegram_approval": True,
            "ota_sync": True,
            "auto_apply": False,
            "audit_log": True,
            "advanced_guardrails": True,
        },
        "description": "Approvazione via Telegram e sync OTA quando il channel manager sara collegato.",
    },
    PLAN_PRO: {
        "label": "Pro",
        "sync_mode": "auto",
        "max_properties": 25,
        "analysis_interval_hours": 6,
        "competitor_limit": 12,
        "telegram_recommendations": True,
        "telegram_approval": False,
        "ota_sync": True,
        "auto_apply": True,
        "features": {
            "pricing_cycle": True,
            "telegram_recommendations": True,
            "telegram_approval": False,
            "ota_sync": True,
            "auto_apply": True,
            "audit_log": True,
            "advanced_guardrails": True,
        },
        "description": "Autopilot completo con report e notifiche operative.",
    },
}


def normalize_plan(plan: str | None) -> str:
    value = (plan or PLAN_FREE).lower().strip()
    return value if value in PLANS else PLAN_FREE


def get_plan(plan: str | None) -> Dict:
    key = normalize_plan(plan)
    return {"key": key, **PLAN_DEFINITIONS[key]}


def get_plan_limit(plan: str | None, limit_name: str, default=None):
    return get_plan(plan).get(limit_name, default)


def can_use_feature(plan: str | None, feature: str) -> bool:
    return bool(get_plan(plan).get("features", {}).get(feature, False))


def effective_sync_mode(plan: str | None, requested_mode: str | None = None) -> str:
    """
    Returns the runtime mode allowed by the plan.

    Free is intentionally forced to advisory. Plus and Pro map to the intended
    product behavior even if OTA sync is still a future integration.
    """
    key = normalize_plan(plan)
    if key == PLAN_FREE:
        return "advisory"
    if key == PLAN_PLUS:
        return "approval"
    if key == PLAN_PRO:
        return "auto"
    return requested_mode or "advisory"
