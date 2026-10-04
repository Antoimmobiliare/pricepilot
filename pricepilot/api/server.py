"""
PricePilot - FastAPI Server
API REST per integrazione con channel manager, PMS, e frontend esterno.

Avvio:
    pip install fastapi uvicorn
    uvicorn pricepilot.api.server:app --reload --port 8000

Documentazione interattiva: http://localhost:8000/docs
"""
import os
from datetime import date
from typing import List, Optional, Literal

# Il processo API puo gestire task multi-account in modo server-side. La
# dashboard Streamlit, invece, resta vincolata al singolo account autenticato.
os.environ.setdefault("PRICEPILOT_RUNTIME", "api")

try:
    from fastapi import FastAPI, HTTPException, Query, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

from pricepilot.core.database import (
    init_db, get_decision_log, get_operation_runs, get_audit_events,
    get_guardrail_policy, update_guardrail_policy,
    get_account, update_account, get_users,
    get_notification_preferences, update_notification_preferences,
    get_notification_log,
    get_price_calendar, upsert_calendar_price,
    get_telegram_approvals, mark_decision_rejected,
)
from pricepilot.core.supabase_client import is_supabase_configured, get_supabase_admin_client
from pricepilot.core.data_backend import is_supabase_primary
from pricepilot.services.property_service import (
    list_properties, get_property_by_id, create_property,
    update_property, remove_property,
)
from pricepilot.engine.decision_engine import process_decision, approve_decision
from pricepilot.core.scheduler import run_cloud_pricing_cycle, run_pricing_cycle
from pricepilot.providers.registry import (
    get_billing_provider,
    get_event_provider,
    get_market_data_provider,
    get_occupancy_provider,
)
from pricepilot.services.telegram_bot import (
    process_webhook as tg_process_webhook,
    create_property_link, get_webhook_info, set_webhook, is_configured,
    get_webhook_secret, verify_webhook_secret, webhook_secret_required,
    WEBHOOK_SECRET_HEADER,
)
from pricepilot.services.readiness import account_readiness
from pricepilot.services.account_service import (
    edit_user, invite_user, remove_user, update_account_profile,
)
from pricepilot.services.tenant_service import (
    API_KEY_HEADER,
    SCHEDULER_KEY_HEADER,
    api_auth_required,
    configured_api_keys,
    default_account_id,
    production_mode,
    resolve_account_id_from_api_key,
    verify_scheduler_key,
)


if not _FASTAPI_AVAILABLE:
    raise ImportError(
        "FastAPI non installato. Esegui: pip install fastapi uvicorn"
    )

# ─── App ─────────────────────────────────────────────────────────────────────
app = FastAPI(
    title       = "PricePilot API",
    description = "Dynamic Pricing Engine per Affitti Brevi",
    version     = "1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins  = [
        origin.strip()
        for origin in os.getenv("PRICEPILOT_CORS_ORIGINS", "http://localhost:8501").split(",")
        if origin.strip()
    ],
    allow_methods  = ["*"],
    allow_headers  = ["*"],
)

PUBLIC_PATHS = {
    "/health",
    "/ready",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/telegram/webhook",
    "/stripe/webhook",
    "/internal/scheduled-pricing-cycle",
}


@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    request.state.account_id = default_account_id()
    if api_auth_required() and request.url.path not in PUBLIC_PATHS:
        account_id = resolve_account_id_from_api_key(request.headers.get(API_KEY_HEADER, ""))
        if account_id is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "API key mancante o non valida."},
            )
        request.state.account_id = account_id
    return await call_next(request)


def _account_id(request: Request) -> int:
    return int(getattr(request.state, "account_id", default_account_id()))


# Init DB all'avvio
@app.on_event("startup")
def startup():
    init_db()


# ─── Schemas ─────────────────────────────────────────────────────────────────

@app.get("/health", tags=["System"], summary="Stato servizio")
def api_health():
    return {
        "ok": True,
        "service": "pricepilot",
        "revision": os.getenv("RENDER_GIT_COMMIT", os.getenv("GIT_COMMIT", "unknown"))[:12],
    }


@app.get("/ready", tags=["System"], summary="Readiness deploy produzione")
def api_readiness():
    checks = _deployment_readiness_checks()
    return {
        "ok": all(item["ok"] for item in checks.values()),
        "service": "pricepilot",
        "environment": os.getenv("PRICEPILOT_ENV", "development"),
        "checks": checks,
    }


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _api_base_url() -> str:
    for key in ("PRICEPILOT_API_BASE_URL", "APP_API_BASE_URL"):
        value = os.getenv(key, "").strip().rstrip("/")
        if value:
            return value
    return ""


def _deployment_readiness_checks() -> dict:
    from pricepilot.core.data_quality import calendar_pricing_enabled
    from types import SimpleNamespace
    own_calendar = calendar_pricing_enabled()
    prod = production_mode()
    api_keys = configured_api_keys()
    api_base_url = _api_base_url()
    billing_provider = get_billing_provider()
    market_provider = SimpleNamespace(name="not_used_calendar_only") if own_calendar else get_market_data_provider()
    event_provider = SimpleNamespace(name="owner_rules_only") if own_calendar else get_event_provider()
    occupancy_provider = get_occupancy_provider()
    provider_names = {
        "market": getattr(market_provider, "name", type(market_provider).__name__),
        "events": getattr(event_provider, "name", type(event_provider).__name__),
        "occupancy": getattr(occupancy_provider, "name", type(occupancy_provider).__name__),
    }
    demo_data = any(str(name).startswith("demo") for name in provider_names.values())
    market_not_configured = not own_calendar and provider_names["market"] == "competitor_provider_unconfigured"
    inventory_missing = "unconfigured" in provider_names["occupancy"]
    schema_ready = _operational_schema_ready()
    return {
        "api_base_url": {
            "ok": bool(api_base_url) or not prod,
            "required": prod,
            "detail": "PRICEPILOT_API_BASE_URL/APP_API_BASE_URL configurato" if api_base_url else "URL API pubblico mancante",
        },
        "api_auth": {
            "ok": bool(api_keys) or not prod,
            "required": prod,
            "detail": "API key configurate" if api_keys else "API key mancanti",
        },
        "supabase": {
            "ok": is_supabase_configured() or not prod,
            "required": prod,
            "detail": "Supabase configurato" if is_supabase_configured() else "Supabase non configurato",
        },
        "operational_schema": {
            "ok": schema_ready,
            "required": is_supabase_primary(),
            "detail": (
                "Tabelle operative Supabase disponibili"
                if schema_ready else
                "Esegui supabase/prelaunch_finalize.sql nel Supabase SQL Editor"
            ),
        },
        "telegram_webhook_secret": {
            "ok": bool(get_webhook_secret()) or not prod,
            "required": prod,
            "detail": "Telegram webhook secret configurato" if get_webhook_secret() else "Telegram webhook secret mancante",
        },
        "stripe_webhook_secret": {
            "ok": bool(os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()) or not prod,
            "required": prod,
            "detail": "Stripe webhook secret configurato" if os.getenv("STRIPE_WEBHOOK_SECRET", "").strip() else "Stripe webhook secret mancante",
        },
        "billing_provider": {
            "ok": bool(getattr(billing_provider, "is_billing_configured", lambda: False)()) or not prod,
            "required": prod,
            "detail": (
                f"Billing configurato via {getattr(billing_provider, 'name', type(billing_provider).__name__)}"
                if getattr(billing_provider, "is_billing_configured", lambda: False)()
                else "Billing non configurato: imposta Stripe test/live prima della vendita"
            ),
        },
        "data_providers": {
            "ok": (not demo_data and not market_not_configured and not inventory_missing) or not prod,
            "required": prod,
            "detail": (
                f"Provider dati: {provider_names}"
                if not demo_data and not market_not_configured and not inventory_missing
                else (
                    "Provider competitor demo non configurato"
                    if market_not_configured
                    else "Collegare calendario e prenotazioni reali; dati mancanti o demo"
                )
            ),
        },
    }


def _operational_schema_ready() -> bool:
    if not is_supabase_primary():
        return True
    client = get_supabase_admin_client()
    if client is None:
        return False
    for table in ("operational_documents", "pricing_date_locks"):
        try:
            client.table(table).select("account_id").limit(1).execute()
        except Exception:
            return False
    return True


class PropertyCreate(BaseModel):
    name:        str   = Field(..., example="My Airbnb Studio")
    platform:    str   = Field("airbnb", example="airbnb")
    listing_url: str   = Field("", example="https://airbnb.com/rooms/123")
    listing_id:  str   = Field("", example="123456")
    city:        str   = Field("", example="Rome")
    property_type: str = Field("", example="entire_apartment")
    max_guests: Optional[int] = Field(None, ge=1, le=100)
    area_m2: Optional[float] = Field(None, gt=0, le=100000)
    layout_summary: str = Field("")
    latitude:    Optional[float] = None
    longitude:   Optional[float] = None
    min_price:   float = Field(50.0, ge=10)
    max_price:   float = Field(300.0, le=5000)
    plan:        str   = Field("free", example="free")
    sync_mode:   str   = Field("advisory", example="advisory")


class PropertyUpdate(BaseModel):
    name:        Optional[str]   = None
    platform:    Optional[str]   = None
    listing_url: Optional[str]   = None
    listing_id:  Optional[str]   = None
    city:        Optional[str]   = None
    property_type: Optional[str] = None
    max_guests: Optional[int] = Field(None, ge=1, le=100)
    area_m2: Optional[float] = Field(None, gt=0, le=100000)
    layout_summary: Optional[str] = None
    min_price:   Optional[float] = None
    max_price:   Optional[float] = None
    plan:        Optional[str]   = None
    sync_mode:   Optional[str]   = None


class PricingRequest(BaseModel):
    property_id:      int   = Field(1, ge=1)
    occupancy:        float = Field(0.65, ge=0.0, le=1.0)
    target_date:      Optional[str] = None   # YYYY-MM-DD
    event:            str   = Field("", example="conference")
    season_factor:    float = Field(1.0, ge=0.5, le=2.0)
    event_factor:     float = Field(1.0, ge=1.0, le=2.0)
    competitor_count: int   = Field(10, ge=2, le=12)
    data_source:      str   = Field("api_manual", example="api_manual")


class ApprovalRequest(BaseModel):
    log_id: int


class GuardrailUpdate(BaseModel):
    max_change_pct:         Optional[float] = Field(None, ge=0.01, le=1.0)
    require_approval_pct:   Optional[float] = Field(None, ge=0.01, le=1.0)
    min_confidence_auto:    Optional[float] = Field(None, ge=0.0, le=1.0)
    competitor_outlier_pct: Optional[float] = Field(None, ge=0.1, le=2.0)
    max_daily_auto_changes: Optional[int]   = Field(None, ge=1, le=100)
    auto_enabled:           Optional[bool]  = None


class AccountUpdate(BaseModel):
    name:                    Optional[str] = None
    plan:                    Optional[str] = None
    billing_status:          Optional[str] = None
    trial_ends_at:           Optional[str] = None
    current_period_ends_at:  Optional[str] = None
    stripe_customer_id:      Optional[str] = None
    stripe_subscription_id:  Optional[str] = None


class UserCreate(BaseModel):
    email:      str = Field(..., example="host@example.com")
    role:       str = Field("manager", example="manager")
    full_name:  str = Field("", example="Mario Rossi")


class UserUpdate(BaseModel):
    email:      Optional[str] = None
    role:       Optional[str] = None
    full_name:  Optional[str] = None


class NotificationPreferenceUpdate(BaseModel):
    telegram_enabled: Optional[bool] = None
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    daily_digest: Optional[bool] = None
    approval_alerts: Optional[bool] = None
    auto_reports: Optional[bool] = None


class CalendarPriceUpsert(BaseModel):
    property_id: int = Field(..., ge=1)
    date: str = Field(..., example="2026-05-15")
    current_price: float = Field(..., ge=1)
    current_price_source: str = Field("manual", example="manual")
    recommended_price: Optional[float] = None
    status: str = Field("current", example="current")
    notes: str = ""


class LeadTimeBand(BaseModel):
    through_days: int = Field(..., ge=0, le=366)
    low_occupancy: float = Field(..., ge=0, le=1)
    high_occupancy: float = Field(..., ge=0, le=1)
    low_multiplier: float = Field(..., ge=.5, le=1)
    high_multiplier: float = Field(..., ge=1, le=2)


class CalendarPolicyUpdate(BaseModel):
    enabled: bool = False
    reference_price: float = Field(..., gt=0, le=100000)
    checkin_time: str = "15:00"
    timezone: str = "Europe/Rome"
    weekend_multiplier: float = Field(1, ge=.5, le=2)
    break_even: float = Field(0, ge=0, le=100000)
    date_reference_prices: dict[str, float] = Field(default_factory=dict)
    lead_time_bands: list[LeadTimeBand]
    pacing_rule: dict = Field(default_factory=dict)
    minimum_stay_rule: dict = Field(default_factory=dict)


class Beds24ConnectionUpdate(BaseModel):
    provider: Literal["beds24"] = "beds24"
    enabled: bool = False
    beds24_property_id: Optional[int] = Field(None, ge=1)
    room_id: Optional[int] = Field(None, ge=1)
    price_slot: Optional[int] = Field(None, ge=1, le=16)
    currency: Literal["EUR"] = "EUR"
    token_env: str = ""
    refresh_token_env: str = ""
    price_basis: Literal["unknown", "accommodation_only"] = "unknown"


# ─── Routes: Account ────────────────────────────────────────────────────────

@app.get("/account", tags=["Account"], summary="Account corrente")
def api_get_account(request: Request):
    account_id = _account_id(request)
    account = get_account(account_id)
    if not account:
        raise HTTPException(404, f"Account {account_id} not found")
    return account


@app.put("/account", tags=["Account"], summary="Aggiorna account/billing scaffold")
def api_update_account(body: AccountUpdate, request: Request):
    account_id = _account_id(request)
    data = {k: v for k, v in body.dict().items() if v is not None}
    account = update_account_profile(account_id, data)
    if not account:
        raise HTTPException(404, f"Account {account_id} not found")
    return account


@app.post("/stripe/webhook", tags=["Billing"], include_in_schema=False)
async def stripe_webhook(request: Request):
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    provider = get_billing_provider()
    process_webhook = getattr(provider, "process_webhook", None)
    if not callable(process_webhook):
        raise HTTPException(503, "Billing webhook non disponibile.")

    result = process_webhook(payload=payload, signature=signature)
    if not result.ok:
        raise HTTPException(400, result.error or "Webhook Stripe non valido.")
    return {"ok": True, "event_type": result.event_type, "account_id": result.account_id}


@app.get("/account/users", tags=["Account"], summary="Utenti account")
def api_get_users(request: Request):
    return get_users(_account_id(request))


@app.post("/account/users", tags=["Account"], status_code=201, summary="Aggiungi utente")
def api_create_user(body: UserCreate, request: Request):
    try:
        return invite_user(
            account_id=_account_id(request),
            email=body.email,
            role=body.role,
            full_name=body.full_name,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@app.put("/account/users/{user_id}", tags=["Account"], summary="Aggiorna utente")
def api_update_user(user_id: int, body: UserUpdate, request: Request):
    data = {k: v for k, v in body.dict().items() if v is not None}
    try:
        user = edit_user(user_id, data, account_id=_account_id(request))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if not user:
        raise HTTPException(404, f"User {user_id} not found")
    return user


@app.delete("/account/users/{user_id}", tags=["Account"], summary="Rimuovi utente")
def api_delete_user(user_id: int, request: Request):
    try:
        deleted = remove_user(user_id, account_id=_account_id(request))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if not deleted:
        raise HTTPException(404, f"User {user_id} not found")
    return {"deleted": user_id}


@app.get("/account/readiness", tags=["Account"], summary="Checklist configurazione account")
def api_account_readiness(request: Request):
    return account_readiness(_account_id(request))


# ─── Routes: Properties ───────────────────────────────────────────────────────

@app.get("/properties", tags=["Properties"], summary="Lista tutte le proprietà")
def api_list_properties(request: Request):
    return list_properties(account_id=_account_id(request))


@app.get("/properties/{prop_id}", tags=["Properties"], summary="Dettaglio proprietà")
def api_get_property(prop_id: int, request: Request):
    account_id = _account_id(request)
    prop = get_property_by_id(prop_id, account_id=account_id)
    if not prop:
        raise HTTPException(404, f"Property {prop_id} not found")
    return prop


@app.post("/properties", tags=["Properties"], status_code=201, summary="Crea proprietà")
def api_create_property(body: PropertyCreate, request: Request):
    try:
        return create_property({**body.dict(), "account_id": _account_id(request)})
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.put("/properties/{prop_id}", tags=["Properties"], summary="Aggiorna proprietà")
def api_update_property(prop_id: int, body: PropertyUpdate, request: Request):
    account_id = _account_id(request)
    if not get_property_by_id(prop_id, account_id=account_id):
        raise HTTPException(404, f"Property {prop_id} not found")
    data = {k: v for k, v in body.dict().items() if v is not None}
    updated = update_property(prop_id, {**data, "account_id": account_id})
    if not updated:
        raise HTTPException(404, f"Property {prop_id} not found")
    return updated


@app.delete("/properties/{prop_id}", tags=["Properties"], summary="Elimina proprietà")
def api_delete_property(prop_id: int, request: Request):
    account_id = _account_id(request)
    if not get_property_by_id(prop_id, account_id=account_id):
        raise HTTPException(404, f"Property {prop_id} not found")
    if not remove_property(prop_id, account_id=account_id):
        raise HTTPException(404, f"Property {prop_id} not found")
    return {"deleted": prop_id}


# ─── Routes: Pricing ──────────────────────────────────────────────────────────

@app.post("/pricing", tags=["Pricing"], summary="Calcola prezzo raccomandato")
def api_pricing(body: PricingRequest, request: Request):
    """
    Calcola il prezzo ottimale per una proprietà.
    Salva la decisione nel decision_log.
    """
    try:
        account_id = _account_id(request)
        if not get_property_by_id(body.property_id, account_id=account_id):
            raise HTTPException(404, f"Property {body.property_id} not found")
        target = date.fromisoformat(body.target_date) if body.target_date else date.today()
        result = process_decision(
            account_id       = account_id,
            property_id      = body.property_id,
            occupancy        = body.occupancy,
            target_date      = target,
            event            = body.event,
            season_factor    = body.season_factor,
            event_factor     = body.event_factor,
            competitor_count = body.competitor_count,
            data_source      = body.data_source,
            occupancy_source = body.data_source,
        )
        return result
    except ValueError as e:
        raise HTTPException(422, str(e))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500, "Errore interno durante il calcolo del prezzo.") from None


@app.get("/pricing", tags=["Pricing"], summary="Pricing rapido via query params")
def api_pricing_get(
    request: Request,
    property_id: int   = Query(1),
    occupancy:   float = Query(0.65),
    event:       str   = Query(""),
    data_source: str   = Query("api_manual"),
):
    raise HTTPException(405, "GET non esegue pricing: usare POST /pricing per creare una decisione.")


# ─── Routes: Decisions ────────────────────────────────────────────────────────

@app.get("/calendar/prices", tags=["Pricing"], summary="Calendario prezzi interno")
def api_price_calendar(
    request: Request,
    property_id: Optional[int] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    limit: int = Query(180, le=1000),
):
    account_id = _account_id(request)
    if property_id is not None and not get_property_by_id(property_id, account_id=account_id):
        raise HTTPException(404, f"Property {property_id} not found")
    return get_price_calendar(
        account_id=account_id,
        property_id=property_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )


@app.put("/calendar/prices", tags=["Pricing"], summary="Imposta prezzo corrente manuale")
def api_upsert_calendar_price(body: CalendarPriceUpsert, request: Request):
    account_id = _account_id(request)
    if not get_property_by_id(body.property_id, account_id=account_id):
        raise HTTPException(404, f"Property {body.property_id} not found")
    try:
        date.fromisoformat(body.date)
    except ValueError:
        raise HTTPException(422, "date deve essere nel formato YYYY-MM-DD")
    return upsert_calendar_price({
        "account_id": account_id,
        "property_id": body.property_id,
        "date": body.date,
        "current_price": body.current_price,
        "current_price_source": body.current_price_source,
        "recommended_price": body.recommended_price,
        "status": body.status,
        "notes": body.notes,
    })


@app.get("/properties/{property_id}/calendar-policy", tags=["Operations"], summary="Regole calendario")
def api_get_calendar_policy(property_id: int, request: Request):
    account_id = _account_id(request)
    if not get_property_by_id(property_id, account_id=account_id):
        raise HTTPException(404, "Appartamento non trovato.")
    from pricepilot.services.operational_store import get_calendar_policy
    return get_calendar_policy(account_id, property_id)


@app.put("/properties/{property_id}/calendar-policy", tags=["Operations"], summary="Salva regole calendario")
def api_save_calendar_policy(property_id: int, body: CalendarPolicyUpdate, request: Request):
    account_id = _account_id(request)
    if not get_property_by_id(property_id, account_id=account_id):
        raise HTTPException(404, "Appartamento non trovato.")
    from pricepilot.services.operational_store import save_calendar_policy
    try:
        return save_calendar_policy(account_id, property_id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@app.get("/properties/{property_id}/channel-connection", tags=["Operations"], summary="Mapping channel manager")
def api_get_channel_connection(property_id: int, request: Request):
    account_id = _account_id(request)
    if not get_property_by_id(property_id, account_id=account_id):
        raise HTTPException(404, "Appartamento non trovato.")
    from pricepilot.services.operational_store import get_connection
    connection = get_connection(account_id, property_id)
    if not connection:
        return None
    # Only report whether referenced secrets exist; values never leave the server.
    return {**connection,
        "token_configured": bool(connection.get("token_env") and os.getenv(connection["token_env"])),
        "refresh_token_configured": bool(connection.get("refresh_token_env") and os.getenv(connection["refresh_token_env"]))}


@app.put("/properties/{property_id}/channel-connection", tags=["Operations"], summary="Salva mapping channel manager")
def api_save_channel_connection(property_id: int, body: Beds24ConnectionUpdate, request: Request):
    account_id = _account_id(request)
    if not get_property_by_id(property_id, account_id=account_id):
        raise HTTPException(404, "Appartamento non trovato.")
    from pricepilot.services.operational_store import save_connection
    try:
        return save_connection(account_id, property_id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@app.get("/properties/{property_id}/performance", tags=["Analytics"], summary="KPI prenotazioni verificati")
def api_property_performance(property_id: int, request: Request, date_from: date, date_to: date):
    account_id = _account_id(request)
    if not get_property_by_id(property_id, account_id=account_id):
        raise HTTPException(404, "Appartamento non trovato.")
    if date_to <= date_from or (date_to-date_from).days > 366:
        raise HTTPException(422, "Intervallo non valido: massimo 366 giorni, fine esclusiva.")
    from pricepilot.services.operational_store import get_reservation_metrics
    return get_reservation_metrics(account_id, property_id, date_from, date_to)


@app.get("/decisions", tags=["Decisions"], summary="Log decisioni")
def api_decisions(
    request: Request,
    property_id: Optional[int] = Query(None),
    limit:       int           = Query(50, le=500),
):
    account_id = _account_id(request)
    if property_id is not None and not get_property_by_id(property_id, account_id=account_id):
        raise HTTPException(404, f"Property {property_id} not found")
    return get_decision_log(limit=limit, property_id=property_id, account_id=account_id)


@app.post("/decision/approve", tags=["Decisions"], summary="Approva decisione pending")
def api_approve(body: ApprovalRequest, request: Request):
    """
    Approva una decisione in modalità 'approval'.
    In produzione aggiorna il listing via channel manager API.
    """
    result = approve_decision(body.log_id, account_id=_account_id(request))
    return {"log_id": body.log_id, **result}


@app.post("/decision/reject", tags=["Decisions"], summary="Rifiuta decisione pending")
def api_reject(body: ApprovalRequest, request: Request):
    rejected = mark_decision_rejected(body.log_id, account_id=_account_id(request))
    if not rejected:
        raise HTTPException(409, "Decisione non disponibile o non più in attesa.")
    return {"log_id": body.log_id, "status": "rejected"}


# ─── Routes: Operations ──────────────────────────────────────────────────────

@app.post("/operations/pricing-cycle", tags=["Operations"], summary="Esegue un ciclo pricing")
def api_run_pricing_cycle(request: Request):
    account_id = _account_id(request)
    account = get_account(account_id) or {}
    if not get_billing_provider().can_run_manual_cycle(account=account, user=None):
        raise HTTPException(403, "Il ciclo manuale e disponibile solo in test/admin.")
    return run_pricing_cycle(account_id=account_id, source="api_manual")


@app.post(
    "/internal/scheduled-pricing-cycle",
    tags=["Operations"],
    include_in_schema=False,
)
def api_run_scheduled_pricing_cycle(request: Request):
    """Trigger interno per il job cloud, separato dalle API dei singoli utenti."""
    if not verify_scheduler_key(request.headers.get(SCHEDULER_KEY_HEADER)):
        raise HTTPException(401, "Chiave scheduler mancante o non valida.")
    return run_cloud_pricing_cycle(source="github_actions")


@app.get("/operations/runs", tags=["Operations"], summary="Storico cicli operativi")
def api_operation_runs(
    request: Request,
    limit: int = Query(50, le=200),
):
    return get_operation_runs(limit=limit, account_id=_account_id(request))


@app.get("/operations/audit", tags=["Operations"], summary="Audit eventi")
def api_audit_events(
    request: Request,
    property_id: Optional[int] = Query(None),
    limit: int = Query(100, le=500),
):
    account_id = _account_id(request)
    if property_id is not None and not get_property_by_id(property_id, account_id=account_id):
        raise HTTPException(404, f"Property {property_id} not found")
    return get_audit_events(limit=limit, account_id=account_id, property_id=property_id)


@app.get("/guardrails", tags=["Operations"], summary="Policy guardrail")
def api_get_guardrails(request: Request, property_id: Optional[int] = Query(None)):
    account_id = _account_id(request)
    if property_id is not None and not get_property_by_id(property_id, account_id=account_id):
        raise HTTPException(404, f"Property {property_id} not found")
    return get_guardrail_policy(account_id=account_id, property_id=property_id)


@app.put("/guardrails", tags=["Operations"], summary="Aggiorna policy guardrail")
def api_update_guardrails(body: GuardrailUpdate, request: Request, property_id: int = Query(0)):
    account_id = _account_id(request)
    if property_id and not get_property_by_id(property_id, account_id=account_id):
        raise HTTPException(404, f"Property {property_id} not found")
    data = {k: v for k, v in body.dict().items() if v is not None}
    if "auto_enabled" in data:
        data["auto_enabled"] = int(bool(data["auto_enabled"]))
    return update_guardrail_policy(account_id=account_id, property_id=property_id, data=data)


@app.get("/notifications/preferences", tags=["Notifications"], summary="Preferenze notifiche")
def api_get_notification_preferences(request: Request, property_id: Optional[int] = Query(None)):
    account_id = _account_id(request)
    if property_id is not None and not get_property_by_id(property_id, account_id=account_id):
        raise HTTPException(404, f"Property {property_id} not found")
    return get_notification_preferences(account_id=account_id, property_id=property_id)


@app.put("/notifications/preferences", tags=["Notifications"], summary="Aggiorna preferenze notifiche")
def api_update_notification_preferences(
    body: NotificationPreferenceUpdate,
    request: Request,
    property_id: int = Query(0),
):
    account_id = _account_id(request)
    if property_id and not get_property_by_id(property_id, account_id=account_id):
        raise HTTPException(404, f"Property {property_id} not found")
    data = {k: v for k, v in body.dict().items() if v is not None}
    for key in ("telegram_enabled", "daily_digest", "approval_alerts", "auto_reports"):
        if key in data:
            data[key] = int(bool(data[key]))
    return update_notification_preferences(account_id=account_id, property_id=property_id, data=data)


@app.get("/notifications/log", tags=["Notifications"], summary="Log notifiche")
def api_notification_log(
    request: Request,
    property_id: Optional[int] = Query(None),
    limit: int = Query(100, le=500),
):
    account_id = _account_id(request)
    if property_id is not None and not get_property_by_id(property_id, account_id=account_id):
        raise HTTPException(404, f"Property {property_id} not found")
    return get_notification_log(limit=limit, account_id=account_id, property_id=property_id)


# ─── Routes: Market ───────────────────────────────────────────────────────────

@app.get("/market/{property_id}", tags=["Market"], summary="Analisi di mercato")
def api_market(
    property_id: int,
    request: Request,
    event: str = Query(""),
    competitor_count: int = Query(10, ge=2, le=12),
):
    account_id = _account_id(request)
    if not get_property_by_id(property_id, account_id=account_id):
        raise HTTPException(404, f"Property {property_id} not found")
    result = get_market_data_provider().analyze(
        property_id=property_id,
        target_date=date.today(),
        event=event,
        competitor_count=competitor_count,
        persist=False,
        account_id=account_id,
        source="api_manual",
    )
    return result.raw or {
        "competitors": result.competitors,
        "market_stats": result.market_stats,
        "source": result.source,
    }


# ─── Routes: Telegram ─────────────────────────────────────────────────────────

@app.get("/market/{property_id}/stays", tags=["Market"], summary="Offerte osservate per durata soggiorno")
def api_market_stays(property_id: int, request: Request, check_in: date,
                     nights: int = Query(1, ge=1, le=30), guests: int = Query(2, ge=1, le=30)):
    account_id = _account_id(request)
    if not get_property_by_id(property_id, account_id=account_id):
        raise HTTPException(404, "Proprieta non disponibile.")
    provider = get_market_data_provider()
    if not hasattr(provider, "analyze_stay"):
        raise HTTPException(503, "Fonte di preventivi per soggiorno non configurata.")
    try:
        return provider.analyze_stay(account_id=account_id, property_id=property_id,
                                     check_in=check_in, nights=nights, guests=guests)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None

@app.post("/telegram/webhook", tags=["Telegram"], include_in_schema=False)
async def telegram_webhook(request: Request):
    """
    Endpoint per il webhook Telegram.
    Configurare con: POST https://api.telegram.org/bot<TOKEN>/setWebhook?url=<PRICEPILOT_API_BASE_URL>/telegram/webhook
    """
    try:
        if webhook_secret_required() and not get_webhook_secret():
            raise HTTPException(503, "Telegram webhook secret non configurato.")
        if not verify_webhook_secret(request.headers.get(WEBHOOK_SECRET_HEADER)):
            raise HTTPException(401, "Telegram webhook secret non valido.")
        update = await request.json()
        tg_process_webhook(update)
        return {"ok": True}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500, "Errore interno durante la gestione del webhook Telegram.") from None


@app.post("/telegram/link/{prop_id}", tags=["Telegram"],
          summary="Genera deep link per collegare proprietà a Telegram")
def api_telegram_link(prop_id: int, request: Request):
    """
    Genera un token e un deep link Telegram per la proprietà.
    L'utente aprirà il link su Telegram per collegare il proprio account.
    """
    if not is_configured():
        raise HTTPException(503, "Telegram bot non configurato (TELEGRAM_BOT_TOKEN mancante)")
    if not get_property_by_id(prop_id, account_id=_account_id(request)):
        raise HTTPException(404, f"Property {prop_id} not found")
    return create_property_link(prop_id)


@app.get("/telegram/status", tags=["Telegram"], summary="Stato bot Telegram")
def api_telegram_status():
    """Ritorna lo stato del bot e le info del webhook."""
    return {
        "configured": is_configured(),
        "webhook":    get_webhook_info() if is_configured() else None,
    }


@app.get("/telegram/approvals", tags=["Telegram"], summary="Storico approvazioni Telegram")
def api_telegram_approvals(
    request: Request,
    property_id: Optional[int] = Query(None),
    limit: int = Query(100, le=500),
):
    account_id = _account_id(request)
    if property_id is not None and not get_property_by_id(property_id, account_id=account_id):
        raise HTTPException(404, f"Property {property_id} not found")
    return get_telegram_approvals(
        limit=limit,
        account_id=account_id,
        property_id=property_id,
    )


@app.on_event("startup")
def _register_webhook_if_needed():
    """Registra il webhook Telegram solo quando il deploy API lo richiede."""
    if not _env_flag("PRICEPILOT_AUTO_REGISTER_TELEGRAM_WEBHOOK"):
        return
    if not is_configured():
        print("Telegram webhook non registrato: TELEGRAM_BOT_TOKEN mancante.")
        return
    if production_mode() and not get_webhook_secret():
        print("Telegram webhook non registrato: TELEGRAM_WEBHOOK_SECRET mancante in produzione.")
        return

    base_url = _api_base_url()
    if not base_url:
        print("Telegram webhook non registrato: PRICEPILOT_API_BASE_URL mancante.")
        return

    result = set_webhook(base_url)
    if result.get("ok"):
        print(f"Telegram webhook registrato: {base_url}/telegram/webhook")
    else:
        print(f"setWebhook fallito: {result.get('description', result)}")


# ─── Health ───────────────────────────────────────────────────────────────────
