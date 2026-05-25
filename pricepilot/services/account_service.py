"""
PricePilot - Account and user service.

Today this runs in local/demo mode with account_id=1. The same boundaries will
be used later by real auth: account owns properties, users belong to account.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from pricepilot.core.database import (
    create_account,
    create_user,
    delete_user,
    get_account,
    get_user,
    get_user_by_email,
    get_users,
    record_audit_event,
    update_account,
    update_user,
)
from pricepilot.core.plans import normalize_plan
from pricepilot.services.tenant_service import default_account_id


ROLES = ("owner", "manager", "viewer")

ROLE_LABELS = {
    "owner": "Owner - gestisce account, piano, utenti e proprieta",
    "manager": "Manager - gestisce proprieta e decisioni",
    "viewer": "Viewer - vede report e storico",
}


def get_current_account_id() -> int:
    return default_account_id()


def get_current_user(account_id: int = 1) -> Optional[Dict]:
    users = list_account_users(account_id)
    return next((u for u in users if u.get("role") == "owner"), users[0] if users else None)


def get_account_context(account_id: int = 1) -> Dict:
    return {
        "account": get_account(account_id),
        "current_user": get_current_user(account_id),
        "users": list_account_users(account_id),
    }


def list_account_users(account_id: int = 1) -> List[Dict]:
    return get_users(account_id)


def invite_user(account_id: int, email: str, role: str = "manager", full_name: str = "") -> Dict:
    role = _normalize_role(role)
    if get_user_by_email(email):
        raise ValueError("Esiste gia un utente con questa email.")
    user = create_user(account_id, email=email, role=role, full_name=full_name)
    record_audit_event(
        action="user_added",
        entity_type="user",
        entity_id=user["id"],
        account_id=account_id,
        source="dashboard_or_api",
        status="ok",
        details={"email": user["email"], "role": user["role"]},
    )
    return user


def create_account_owner(
    email: str,
    password_hash: str,
    account_name: str = "",
    full_name: str = "",
    plan: str = "free",
) -> Dict:
    """Crea account + owner locale. Usato dalla registrazione senza provider esterno."""
    if get_user_by_email(email):
        raise ValueError("Esiste gia un utente con questa email.")
    account = create_account(
        account_name or "La mia attivita",
        plan=normalize_plan(plan),
        billing_status="dev",
    )
    user = create_user(
        int(account["id"]),
        email=email,
        role="owner",
        full_name=full_name,
    )
    user = update_user(user["id"], {
        "password_hash": password_hash,
        "auth_provider": "local",
    }) or user
    record_audit_event(
        action="account_created",
        entity_type="account",
        entity_id=account["id"],
        account_id=int(account["id"]),
        source="local_auth",
        status="ok",
        details={"email": email},
    )
    return {"account": account, "user": user}


def edit_user(user_id: int, data: Dict, account_id: Optional[int] = None) -> Optional[Dict]:
    existing = get_user(user_id)
    if not existing:
        return None
    if account_id is not None and int(existing.get("account_id") or 1) != int(account_id):
        return None
    if "role" in data:
        data["role"] = _normalize_role(data["role"])
    updated = update_user(user_id, data)
    if updated:
        record_audit_event(
            action="user_updated",
            entity_type="user",
            entity_id=user_id,
            account_id=int(updated.get("account_id") or 1),
            source="dashboard_or_api",
            status="ok",
            details={"email": updated.get("email"), "role": updated.get("role")},
        )
    return updated


def remove_user(user_id: int, account_id: Optional[int] = None) -> bool:
    existing = get_user(user_id)
    if not existing:
        return False
    if account_id is not None and int(existing.get("account_id") or 1) != int(account_id):
        return False
    ok = delete_user(user_id)
    if ok:
        record_audit_event(
            action="user_removed",
            entity_type="user",
            entity_id=user_id,
            account_id=int(existing.get("account_id") or 1),
            source="dashboard_or_api",
            status="ok",
            details={"email": existing.get("email"), "role": existing.get("role")},
        )
    return ok


def update_account_profile(account_id: int, data: Dict) -> Optional[Dict]:
    if "plan" in data:
        data["plan"] = normalize_plan(data.get("plan"))
    updated = update_account(account_id, data)
    if updated:
        record_audit_event(
            action="account_updated",
            entity_type="account",
            entity_id=account_id,
            account_id=account_id,
            source="dashboard_or_api",
            status="ok",
            details={"name": updated.get("name"), "plan": updated.get("plan")},
        )
    return updated


def _normalize_role(role: str) -> str:
    value = (role or "manager").strip().lower()
    if value not in ROLES:
        raise ValueError(f"Ruolo non valido. Usa uno tra: {', '.join(ROLES)}")
    return value
