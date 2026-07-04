"""
Stripe-ready billing provider.

This adapter is intentionally optional: PricePilot can run without Stripe
installed or configured, while production can enable checkout by setting env
vars and installing the Stripe package from requirements.txt.
"""
from __future__ import annotations

import importlib
import json
import os
from datetime import datetime, timezone
from typing import Optional

from pricepilot.core.plans import get_plan, normalize_plan
from pricepilot.providers.contracts import (
    BillingCheckoutResult,
    BillingPlanResult,
    BillingWebhookResult,
)


class StripeBillingProvider:
    name = "stripe"

    def __init__(self) -> None:
        self.secret_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
        self.webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
        self.price_ids = {
            "plus": os.environ.get("STRIPE_PRICE_PLUS", "").strip(),
            "pro": os.environ.get("STRIPE_PRICE_PRO", "").strip(),
        }

    def get_account_plan(self, *, account_id: int) -> BillingPlanResult:
        from pricepilot.core.database import get_account

        account = get_account(account_id) or {"plan": "free", "billing_status": "dev"}
        status = str(account.get("billing_status") or "dev").lower()
        plan = normalize_plan(account.get("plan") if status in {"active", "trialing", "dev"} else "free")
        plan_info = get_plan(plan)
        return BillingPlanResult(
            plan=plan,
            billing_status=status,
            features=plan_info.get("features", {}),
            raw={"account": account, "plan": plan_info, "provider": self.name},
        )

    def can_run_manual_cycle(self, *, account: dict, user: Optional[dict] = None) -> bool:
        user = user or {}
        status = str(account.get("billing_status", "")).lower()
        return (
            status in {"active", "trialing", "dev"}
            or str(user.get("role", "")).lower() == "admin"
            or os.environ.get("PRICEPILOT_ALLOW_MANUAL_CYCLE", "").strip() == "1"
        )

    def is_billing_configured(self) -> bool:
        return bool(self.secret_key and self.price_ids.get("plus") and self.price_ids.get("pro"))

    def create_checkout_session(
        self,
        *,
        account_id: int,
        plan: str,
        success_url: str = "",
        cancel_url: str = "",
    ) -> BillingCheckoutResult:
        plan = normalize_plan(plan)
        if plan == "free":
            return BillingCheckoutResult(
                ok=True,
                plan=plan,
                provider=self.name,
                url=success_url or self._default_success_url(),
                raw={"account_id": account_id, "free_plan": True},
            )

        price_id = self.price_ids.get(plan, "")
        if not self.secret_key or not price_id:
            return BillingCheckoutResult(
                ok=False,
                plan=plan,
                provider=self.name,
                error="Stripe non configurato: aggiungi STRIPE_SECRET_KEY e STRIPE_PRICE_PLUS/PRO.",
                raw={"account_id": account_id, "price_id_present": bool(price_id)},
            )

        stripe = self._stripe_module()
        if stripe is None:
            return BillingCheckoutResult(
                ok=False,
                plan=plan,
                provider=self.name,
                error="Pacchetto stripe non installato. Esegui pip install -r requirements.txt.",
            )

        from pricepilot.core.database import get_account

        account = get_account(account_id) or {}
        stripe.api_key = self.secret_key
        payload = {
            "mode": "subscription",
            "line_items": [{"price": price_id, "quantity": 1}],
            "client_reference_id": str(account_id),
            "success_url": success_url or self._default_success_url(),
            "cancel_url": cancel_url or self._default_cancel_url(),
            "metadata": {"account_id": str(account_id), "plan": plan},
            "subscription_data": {"metadata": {"account_id": str(account_id), "plan": plan}},
        }
        customer_id = str(account.get("stripe_customer_id") or "").strip()
        if customer_id:
            payload["customer"] = customer_id
        elif account.get("name"):
            payload["customer_creation"] = "always"

        try:
            session = stripe.checkout.Session.create(**payload)
            session_id = self._read_attr(session, "id")
            url = self._read_attr(session, "url")
            return BillingCheckoutResult(
                ok=bool(url),
                plan=plan,
                provider=self.name,
                url=url,
                raw={"session_id": session_id, "account_id": account_id},
            )
        except Exception as exc:
            return BillingCheckoutResult(
                ok=False,
                plan=plan,
                provider=self.name,
                error=f"Errore checkout Stripe: {exc}",
                raw={"account_id": account_id},
            )

    def create_customer_portal(
        self,
        *,
        account_id: int,
        return_url: str = "",
    ) -> BillingCheckoutResult:
        if not self.secret_key:
            return BillingCheckoutResult(
                ok=False,
                provider=self.name,
                error="Stripe non configurato: manca STRIPE_SECRET_KEY.",
            )

        stripe = self._stripe_module()
        if stripe is None:
            return BillingCheckoutResult(
                ok=False,
                provider=self.name,
                error="Pacchetto stripe non installato. Esegui pip install -r requirements.txt.",
            )

        from pricepilot.core.database import get_account

        account = get_account(account_id) or {}
        customer_id = str(account.get("stripe_customer_id") or "").strip()
        if not customer_id:
            return BillingCheckoutResult(
                ok=False,
                provider=self.name,
                error="Account senza customer Stripe. Crea prima un checkout.",
                raw={"account_id": account_id},
            )

        stripe.api_key = self.secret_key
        try:
            session = stripe.billing_portal.Session.create(
                customer=customer_id,
                return_url=return_url or self._default_portal_return_url(),
            )
            return BillingCheckoutResult(
                ok=True,
                provider=self.name,
                url=self._read_attr(session, "url"),
                raw={"session_id": self._read_attr(session, "id"), "account_id": account_id},
            )
        except Exception as exc:
            return BillingCheckoutResult(
                ok=False,
                provider=self.name,
                error=f"Errore Customer Portal Stripe: {exc}",
                raw={"account_id": account_id},
            )

    def process_webhook(
        self,
        *,
        payload: bytes,
        signature: str = "",
    ) -> BillingWebhookResult:
        stripe = self._stripe_module()
        if stripe is None:
            return BillingWebhookResult(
                ok=False,
                provider=self.name,
                error="Pacchetto stripe non installato. Esegui pip install -r requirements.txt.",
            )

        try:
            event = self._construct_webhook_event(stripe, payload, signature)
        except Exception as exc:
            return BillingWebhookResult(
                ok=False,
                provider=self.name,
                error=f"Webhook Stripe non valido: {exc}",
            )

        event_type = self._read_attr(event, "type")
        obj = self._read_path(event, "data.object") or {}
        if event_type not in {
            "checkout.session.completed",
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        }:
            return BillingWebhookResult(
                ok=True,
                provider=self.name,
                event_type=event_type,
                raw={"ignored": True},
            )

        account_id = self._account_id_from_object(obj)
        if not account_id:
            return BillingWebhookResult(
                ok=False,
                provider=self.name,
                event_type=event_type,
                error="Evento Stripe senza account_id metadata/client_reference_id.",
            )

        status = self._billing_status_for_event(event_type, obj)
        plan = self._plan_from_object(obj)
        effective_plan = plan if status in {"active", "trialing", "dev"} else "free"
        customer_id = self._read_attr(obj, "customer")
        subscription_id = self._subscription_id_from_object(obj)
        period_end = self._timestamp_to_iso(self._read_path(obj, "current_period_end"))
        trial_end = self._timestamp_to_iso(self._read_path(obj, "trial_end"))

        from pricepilot.core.database import get_account, record_audit_event, update_account
        from pricepilot.services.supabase_repository import sync_account_to_supabase

        existing = get_account(account_id)
        if not existing:
            return BillingWebhookResult(
                ok=False,
                provider=self.name,
                event_type=event_type,
                account_id=account_id,
                error=f"Account {account_id} non trovato.",
            )

        updated = update_account(account_id, {
            "plan": effective_plan,
            "billing_status": status,
            "trial_ends_at": trial_end or existing.get("trial_ends_at"),
            "current_period_ends_at": period_end or existing.get("current_period_ends_at"),
            "stripe_customer_id": customer_id or existing.get("stripe_customer_id", ""),
            "stripe_subscription_id": subscription_id or existing.get("stripe_subscription_id", ""),
        }) or existing
        sync_account_to_supabase(updated)
        record_audit_event(
            action="billing_webhook_processed",
            entity_type="account",
            entity_id=account_id,
            account_id=account_id,
            source="stripe",
            status="ok",
            details={
                "event_type": event_type,
                "plan": effective_plan,
                "billing_status": status,
                "stripe_customer_id": customer_id,
                "stripe_subscription_id": subscription_id,
            },
        )
        return BillingWebhookResult(
            ok=True,
            provider=self.name,
            event_type=event_type,
            account_id=account_id,
            plan=effective_plan,
            billing_status=status,
            raw={"requested_plan": plan},
        )

    def _stripe_module(self):
        try:
            return importlib.import_module("stripe")
        except Exception:
            return None

    def _construct_webhook_event(self, stripe, payload: bytes, signature: str):
        if self.webhook_secret:
            if not signature:
                raise ValueError("firma Stripe mancante")
            return stripe.Webhook.construct_event(payload, signature, self.webhook_secret)
        if os.environ.get("PRICEPILOT_ENV", "").strip().lower() == "production":
            raise ValueError("STRIPE_WEBHOOK_SECRET obbligatorio in produzione")
        return json.loads((payload or b"{}").decode("utf-8"))

    def _account_id_from_object(self, obj) -> int:
        metadata = self._read_attr(obj, "metadata")
        raw = ""
        if isinstance(metadata, dict):
            raw = metadata.get("account_id") or ""
        raw = raw or self._read_attr(obj, "client_reference_id")
        try:
            return max(0, int(raw or 0))
        except (TypeError, ValueError):
            return 0

    def _plan_from_object(self, obj) -> str:
        metadata = self._read_attr(obj, "metadata")
        if isinstance(metadata, dict):
            plan = normalize_plan(metadata.get("plan"))
            if plan != "free":
                return plan

        price_id = (
            self._read_path(obj, "items.data.0.price.id")
            or self._read_path(obj, "lines.data.0.price.id")
        )
        for plan, configured_price_id in self.price_ids.items():
            if configured_price_id and configured_price_id == price_id:
                return plan
        return "free"

    def _billing_status_for_event(self, event_type: str, obj) -> str:
        if event_type == "customer.subscription.deleted":
            return "canceled"
        status = self._read_attr(obj, "status")
        if status:
            return status.lower()
        if event_type == "checkout.session.completed":
            return "active"
        return "unknown"

    def _subscription_id_from_object(self, obj) -> str:
        return self._read_attr(obj, "subscription") or self._read_attr(obj, "id")

    @staticmethod
    def _timestamp_to_iso(value) -> str:
        try:
            timestamp = int(value or 0)
        except (TypeError, ValueError):
            return ""
        if timestamp <= 0:
            return ""
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()

    def _default_success_url(self) -> str:
        return os.environ.get("STRIPE_SUCCESS_URL", "").strip() or f"{self._app_base_url()}?billing=success"

    def _default_cancel_url(self) -> str:
        return os.environ.get("STRIPE_CANCEL_URL", "").strip() or f"{self._app_base_url()}?billing=cancel"

    def _default_portal_return_url(self) -> str:
        return os.environ.get("STRIPE_PORTAL_RETURN_URL", "").strip() or self._app_base_url()

    def _app_base_url(self) -> str:
        return (
            os.environ.get("APP_BASE_URL", "").strip()
            or os.environ.get("SUPABASE_PASSWORD_RESET_REDIRECT_URL", "").strip()
            or "http://localhost:8501"
        ).rstrip("/")

    @staticmethod
    def _read_attr(obj, key: str) -> str:
        if isinstance(obj, dict):
            value = obj.get(key)
            return value if isinstance(value, dict) else str(value or "")
        value = getattr(obj, key, "")
        return value if isinstance(value, dict) else str(value or "")

    @classmethod
    def _read_path(cls, obj, path: str):
        current = obj
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                try:
                    current = current[int(part)]
                except (TypeError, ValueError, IndexError):
                    return None
            else:
                current = getattr(current, part, None)
            if current is None:
                return None
        return current
