"""
PricePilot - Dashboard Streamlit
Dashboard interattiva per dynamic pricing di affitti brevi.

Avvio: streamlit run pricepilot/dashboard/app.py
"""
import sys
import os
from pathlib import Path

# ── Risoluzione path per import corretto ──────────────────────────────────────
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import html as _html
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import date, datetime, timedelta

from pricepilot.dashboard.auth import (
    get_current_user,
    get_current_account_id,
    require_auth,
    render_logout_button,
)

from pricepilot.core.config import CONFIG, save_config, load_config
from pricepilot.core.plans import effective_sync_mode, get_plan
from pricepilot.core.database import (
    init_db, save_decision, save_competitors,
    save_market_snapshot, get_decisions, get_summary_stats,
)
from pricepilot.pricing.engine import calculate_price
from pricepilot.pricing.strategies import STRATEGIES
from pricepilot.data_sources.competitors import get_competitor_prices, get_market_summary
from pricepilot.data_sources.events import get_upcoming_events, get_event_for_date, event_to_string
from pricepilot.analytics.analyzer import (
    load_decisions_df, price_history_series, price_vs_market_df,
    occupancy_price_correlation, rolling_avg_price, compute_kpis,
    strategy_breakdown, event_impact_analysis, estimate_revenue,
)
from pricepilot.export.exporter import export_csv, export_json

# ── Nuovi moduli v2 ───────────────────────────────────────────────────────────
from pricepilot.engine.market_analyzer import simulate_competitors, calculate_market_stats
from pricepilot.engine.pricing_engine import calculate_recommended_price, is_weekend
from pricepilot.engine.decision_engine import process_decision
from pricepilot.services.property_service import (
    list_properties, get_or_create_default, get_property_by_id,
    create_property, update_property, sync_property_pricing_rules,
)
from pricepilot.core.database import (
    get_decision_log, get_market_history,
    get_all_telegram_links, get_pending_approvals,
    get_account, get_effective_plan_for_property,
    get_last_operation_run, get_operation_runs,
    get_guardrail_policy, update_guardrail_policy,
    get_notification_preferences, update_notification_preferences,
    get_notification_log,
    save_telegram_link, revoke_telegram_link,
    get_property_integrations, upsert_property_integration, delete_property_integration,
    get_current_price_for_date, get_price_calendar, upsert_calendar_price,
)
from pricepilot.services.readiness import account_readiness
from pricepilot.services.account_service import update_account_profile

# ─── Init DB ──────────────────────────────────────────────────────────────────
init_db()


def current_account_id() -> int:
    return get_current_account_id()

# ─── Plotly toolbar config (keep zoom, pan, download PNG only) ────────────────
# Account-scoped Streamlit state guard.
def reset_account_scoped_state_if_needed():
    account_id = current_account_id()
    previous_account_id = st.session_state.get("_pp_active_account_id")
    if previous_account_id == account_id:
        return

    st.session_state["_pp_active_account_id"] = account_id

    exact_keys = {
        "active_prop_id",
        "sidebar_prop_sel",
        "account_name_input",
        "account_plan_select",
        "prop_form_sel",
        "prop_form_prev_sel",
        "pf_name",
        "pf_city",
        "pf_platform",
        "pf_url",
        "pf_lid",
        "pf_strategy",
        "pf_min",
        "pf_max",
        "pf_plan",
        "notif_telegram_enabled",
        "notif_approval_alerts",
        "notif_auto_reports",
        "notif_daily_digest",
        "notif_quiet_start",
        "notif_quiet_end",
        "cal_selected_day",
        "cal_day_input",
    }
    prefixes = (
        "prop_step_",
        "prop_draft_",
        "ota_extras_",
        "ota_loaded_",
        "ota_del_ids_",
        "onb_",
        "tg_link_",
        "integ_show_help_",
        "cal_prices_",
        "cal_hash_",
        "cal_overrides_",
        "cal_locks_",
        "current_price_",
    )

    for key in list(st.session_state.keys()):
        if key in exact_keys or any(str(key).startswith(prefix) for prefix in prefixes):
            st.session_state.pop(key, None)


def _sync_active_property_from_sidebar() -> None:
    selected_id = st.session_state.get("sidebar_prop_sel")
    if selected_id:
        st.session_state["active_prop_id"] = selected_id


def reset_property_pricing_widget_state(
    prop_id: int | str | None,
    *,
    sidebar: bool = True,
    pricing_tab: bool = True,
) -> None:
    """Forza i widget non appena usati a rileggere i limiti salvati sulla proprieta."""
    if not prop_id:
        return
    keys = []
    if sidebar:
        keys.extend([
            f"sidebar_strategy_{prop_id}",
            f"sidebar_min_price_{prop_id}",
            f"sidebar_max_price_{prop_id}",
        ])
    if pricing_tab:
        keys.extend([
            f"ps_min_{prop_id}",
            f"ps_max_{prop_id}",
        ])
    for key in keys:
        for state_key in list(st.session_state.keys()):
            if state_key == key or str(state_key).startswith(f"{key}_"):
                st.session_state.pop(state_key, None)


def queue_property_pricing_widget_reset(
    prop_id: int | str | None,
    *,
    sidebar: bool = True,
    pricing_tab: bool = True,
) -> None:
    """Accoda il reset dei widget per il prossimo render della vista interessata."""
    if not prop_id:
        return
    key = f"pp_pending_price_widget_reset_{prop_id}"
    current = st.session_state.get(key, {"sidebar": False, "pricing_tab": False})
    st.session_state[key] = {
        "sidebar": bool(current.get("sidebar")) or sidebar,
        "pricing_tab": bool(current.get("pricing_tab")) or pricing_tab,
    }


def apply_pending_property_pricing_widget_reset(
    prop_id: int | str | None,
    *,
    sidebar: bool = False,
    pricing_tab: bool = False,
) -> None:
    """Applica reset accodati prima che i widget Streamlit vengano renderizzati."""
    if not prop_id:
        return
    key = f"pp_pending_price_widget_reset_{prop_id}"
    pending = st.session_state.get(key)
    if not pending:
        return

    reset_sidebar = sidebar and bool(pending.get("sidebar"))
    reset_pricing_tab = pricing_tab and bool(pending.get("pricing_tab"))
    if reset_sidebar or reset_pricing_tab:
        reset_property_pricing_widget_state(
            prop_id,
            sidebar=reset_sidebar,
            pricing_tab=reset_pricing_tab,
        )

    remaining = {
        "sidebar": bool(pending.get("sidebar")) and not reset_sidebar,
        "pricing_tab": bool(pending.get("pricing_tab")) and not reset_pricing_tab,
    }
    if remaining["sidebar"] or remaining["pricing_tab"]:
        st.session_state[key] = remaining
    else:
        st.session_state.pop(key, None)


def _price_limit_state_keys(prop_id: int | str) -> tuple[str, str, str]:
    return (
        f"pp_price_min_{prop_id}",
        f"pp_price_max_{prop_id}",
        f"pp_price_limits_saved_{prop_id}",
    )


def _remember_saved_price_limits(prop_id: int | str, min_price: float, max_price: float) -> None:
    """Allinea la cache UI ai limiti salvati della proprieta."""
    min_value = float(min_price)
    max_value = float(max_price)
    min_key, max_key, saved_key = _price_limit_state_keys(prop_id)
    st.session_state[min_key] = min_value
    st.session_state[max_key] = max_value
    st.session_state[saved_key] = (min_value, max_value)


def get_synced_price_limits(prop: dict | None, cfg: dict | None = None) -> tuple[float, float]:
    """Ritorna i limiti prezzo condivisi tra sidebar e tab Prezzi."""
    cfg = cfg or {}
    if not prop:
        return (
            float(cfg.get("min_price_per_night", 50.0)),
            float(cfg.get("max_price_per_night", 500.0)),
        )

    prop_id = prop["id"]
    min_key, max_key, saved_key = _price_limit_state_keys(prop_id)
    saved_min = float(prop.get("min_price", cfg.get("min_price_per_night", 50.0)))
    saved_max = float(prop.get("max_price", cfg.get("max_price_per_night", 500.0)))
    saved_signature = (saved_min, saved_max)

    if st.session_state.get(saved_key) != saved_signature:
        st.session_state[min_key] = saved_min
        st.session_state[max_key] = saved_max
        st.session_state[saved_key] = saved_signature
    else:
        st.session_state.setdefault(min_key, saved_min)
        st.session_state.setdefault(max_key, saved_max)

    return float(st.session_state[min_key]), float(st.session_state[max_key])


def _price_limit_widget_suffix(prop: dict | None, cfg: dict | None = None) -> str:
    """Versione stabile dei widget min/max: cambia solo dopo un salvataggio reale."""
    if not prop:
        return "global"
    min_value, max_value = get_synced_price_limits(prop, cfg)
    return (
        f"{prop['id']}_"
        f"{int(round(float(min_value) * 100))}_"
        f"{int(round(float(max_value) * 100))}"
    )


def save_synced_price_limits(
    prop: dict,
    min_price: float,
    max_price: float,
    *,
    strategy: str | None = None,
    reset_sidebar: bool = True,
    reset_pricing_tab: bool = True,
    pricing_rules: dict | None = None,
) -> dict | None:
    """Salva min/max su proprieta, config e session_state in un unico punto."""
    prop_id = int(prop["id"])
    min_value = float(min_price)
    max_value = float(max_price)
    if min_value >= max_value:
        raise ValueError("Il prezzo minimo deve essere inferiore al prezzo massimo.")

    data = {
        "min_price": min_value,
        "max_price": max_value,
    }
    if strategy is not None:
        data["strategy"] = strategy

    updated_prop = update_property(prop_id, data)
    save_config({
        **load_config(),
        "min_price_per_night": min_value,
        "max_price_per_night": max_value,
        **({"strategy": strategy} if strategy is not None else {}),
    })

    _remember_saved_price_limits(prop_id, min_value, max_value)
    st.session_state["active_prop_id"] = prop_id
    if updated_prop:
        sync_property_pricing_rules(
            updated_prop,
            {
                "min_price": min_value,
                "max_price": max_value,
                **({"strategy": strategy} if strategy is not None else {}),
                **(pricing_rules or {}),
            },
        )
    queue_property_pricing_widget_reset(
        prop_id,
        sidebar=reset_sidebar,
        pricing_tab=reset_pricing_tab,
    )
    st.cache_data.clear()
    return updated_prop


# Plotly toolbar config (keep zoom, pan, download PNG only).
PLOTLY_CONFIG = {
    "modeBarButtonsToRemove": [
        "select2d", "lasso2d", "zoom2d", "autoScale2d", "resetScale2d",
        "toggleSpikelines", "hoverClosestCartesian", "hoverCompareCartesian",
    ],
    "displaylogo": False,
}

# ─── Page Config ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="PricePilot",
    page_icon="✈️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── CSS Custom ──────────────────────────────────────────────────────────────
st.markdown("""
<style>
    /* === Typography === */
    .main-title { font-size: 2.2rem; font-weight: 800; color: #1a1a2e; margin-bottom: 0; }
    .subtitle   { font-size: 1rem; color: #666; margin-top: 0; margin-bottom: 1.5rem; }
    .section-title { font-size: 1.2rem; font-weight: 700; color: #1a1a2e;
                     border-left: 4px solid #667eea; padding-left: 10px;
                     margin: 1.5rem 0 0.8rem; }

    /* === Price Card === */
    .price-card {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        border-radius: 16px; padding: 24px; color: white; text-align: center;
    }
    .price-big   { font-size: 3rem; font-weight: 900; margin: 8px 0; }
    .price-label { font-size: 0.9rem; opacity: 0.85; text-transform: uppercase; }

    /* === Metric Card === */
    .metric-card {
        background: white; border-radius: 12px; padding: 16px;
        border: 1px solid #e8e8e8; text-align: center;
        box-shadow: 0 2px 8px rgba(0,0,0,.06);
    }

    /* === KPI Cards – colori semantici === */
    /* verde = positivo / applicato */
    .kpi-green { background: linear-gradient(135deg,#d4edda,#c3e6cb);
                 border:1px solid #28a74533; border-radius:14px; padding:20px;
                 text-align:center; box-shadow:0 2px 10px rgba(0,0,0,.06); }
    /* blu = informazione neutra */
    .kpi-blue  { background: linear-gradient(135deg,#cce5ff,#b8daff);
                 border:1px solid #007bff33; border-radius:14px; padding:20px;
                 text-align:center; box-shadow:0 2px 10px rgba(0,0,0,.06); }
    /* giallo = attenzione / alert */
    .kpi-yellow{ background: linear-gradient(135deg,#fff3cd,#ffeeba);
                 border:1px solid #ffc10733; border-radius:14px; padding:20px;
                 text-align:center; box-shadow:0 2px 10px rgba(0,0,0,.06); }
    /* viola = dati/storico */
    .kpi-purple{ background: linear-gradient(135deg,#e8e0f8,#d4c5f5);
                 border:1px solid #764ba233; border-radius:14px; padding:20px;
                 text-align:center; box-shadow:0 2px 10px rgba(0,0,0,.06); }
    .kpi-value { font-size:2rem; font-weight:900; margin:6px 0; }
    .kpi-label { font-size:0.78rem; text-transform:uppercase; font-weight:600; opacity:0.7; }
    .kpi-sub   { font-size:0.85rem; margin-top:4px; font-weight:500; }

    /* === Market Badges === */
    .market-badge {
        display: inline-block; padding: 4px 10px; border-radius: 20px;
        font-size: 0.85rem; font-weight: 600;
    }
    .below { background: #d4edda; color: #155724; }
    .above { background: #f8d7da; color: #721c24; }
    .at    { background: #d1ecf1; color: #0c5460; }

    /* === Badge status colorati === */
    /* verde = applicato / successo */
    .badge-green  { background:#d4edda; color:#155724; padding:3px 10px;
                    border-radius:20px; font-size:0.78rem; font-weight:600; }
    /* giallo = in attesa */
    .badge-yellow { background:#fff3cd; color:#856404; padding:3px 10px;
                    border-radius:20px; font-size:0.78rem; font-weight:600; }
    /* rosso = rifiutato / rischio */
    .badge-red    { background:#f8d7da; color:#721c24; padding:3px 10px;
                    border-radius:20px; font-size:0.78rem; font-weight:600; }
    /* blu = informazione */
    .badge-blue   { background:#cce5ff; color:#004085; padding:3px 10px;
                    border-radius:20px; font-size:0.78rem; font-weight:600; }
    /* viola = premium */
    .badge-purple { background:#ede9fe; color:#5b21b6; padding:3px 10px;
                    border-radius:20px; font-size:0.78rem; font-weight:600; }

    /* === Alert Cards (bordo laterale colorato) === */
    .alert-red    { background:#fff5f5; border-left:4px solid #fc8181;
                    border-radius:8px; padding:12px 16px; margin-bottom:8px; }
    .alert-yellow { background:#fffbeb; border-left:4px solid #f6e05e;
                    border-radius:8px; padding:12px 16px; margin-bottom:8px; }
    .alert-green  { background:#f0fff4; border-left:4px solid #68d391;
                    border-radius:8px; padding:12px 16px; margin-bottom:8px; }
    .alert-blue   { background:#ebf8ff; border-left:4px solid #63b3ed;
                    border-radius:8px; padding:12px 16px; margin-bottom:8px; }

    /* === Telegram preview === */
    .tg-preview { background:#1e2936; color:#f0f4f8; border-radius:16px;
                  padding:22px 26px; font-size:0.87rem; line-height:2.0; margin:10px 0;
                  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                  box-shadow: 0 4px 16px rgba(0,0,0,0.25); }
    .tg-preview-header { font-size:1.0rem; font-weight:700; color:#ffffff;
                         margin-bottom:12px; letter-spacing:0.02em; }
    .tg-preview-divider { border:none; border-top:1px solid #3a4a5c; margin:10px 0; }
    .tg-preview-row { display:flex; align-items:baseline; gap:8px; margin:3px 0; }
    .tg-preview-label { color:#8fa8c8; font-size:0.82rem; white-space:nowrap; }
    .tg-preview-value { color:#e8f0fb; font-weight:600; font-size:0.88rem; }
    .tg-preview-question { color:#c8d8ec; font-size:0.85rem; margin-top:12px; }
    .tg-btn-row { display:flex; gap:10px; margin-top:14px; }
    .tg-btn-green { background:linear-gradient(135deg,#2ecc71,#27ae60); color:white;
                    border-radius:8px; padding:7px 18px; font-size:0.82rem; font-weight:700;
                    display:inline-block; letter-spacing:0.03em; }
    .tg-btn-red   { background:linear-gradient(135deg,#e74c3c,#c0392b); color:white;
                    border-radius:8px; padding:7px 18px; font-size:0.82rem; font-weight:700;
                    display:inline-block; letter-spacing:0.03em; }

    /* === Step form header === */
    .step-header { background:linear-gradient(135deg,#667eea,#764ba2);
                   color:white; border-radius:12px; padding:14px 20px; margin-bottom:16px; }

    /* === Strategy card (per form step 2) === */
    .strategy-card { border:2px solid #e8e8e8; border-radius:10px; padding:14px 16px;
                     margin-bottom:8px; cursor:pointer; transition:border-color .2s; }
    .strategy-card-sel { border-color:#667eea; background:#f8f7ff; }

    /* === Onboarding === */
    .onboarding-hero { text-align:center; padding:50px 20px;
                       background:linear-gradient(135deg,#f8faff 0%,#f0f4ff 100%);
                       border-radius:20px; margin:20px 0; }
    .onboarding-step { background:white; border-radius:10px; padding:14px 18px;
                       border:1px solid #e8e8e8; margin:6px 0;
                       display:flex; align-items:center; gap:14px; }

    /* === Onboarding wizard (new) === */
    .onb-card { background:white; border-radius:20px; padding:40px 48px;
                box-shadow:0 4px 24px rgba(102,126,234,.10);
                border:1px solid #e8ecff; max-width:620px; margin:0 auto; }
    .onb-progress-track { background:#e8ecff; border-radius:99px;
                          height:6px; margin-bottom:32px; }
    .onb-progress-fill  { background:linear-gradient(90deg,#667eea,#764ba2);
                          border-radius:99px; height:6px; transition:width .4s; }
    .onb-step-label { font-size:0.72rem; font-weight:700; color:#667eea;
                      text-transform:uppercase; letter-spacing:.08em; margin-bottom:6px; }
    .onb-title { font-size:1.55rem; font-weight:800; color:#1a1a2e; margin:0 0 8px; }
    .onb-desc  { font-size:0.95rem; color:#6b7280; margin:0 0 28px; line-height:1.6; }
    .onb-found-card { background:#f0fdf4; border:1.5px solid #86efac;
                      border-radius:12px; padding:16px 20px; margin:12px 0 20px; }
    .onb-found-row  { display:flex; gap:8px; align-items:baseline; margin:4px 0; }
    .onb-found-label{ font-size:0.78rem; color:#6b7280; min-width:100px; }
    .onb-found-value{ font-size:0.95rem; font-weight:700; color:#1a1a2e; }
    .onb-suggestion { background:#eff6ff; border:1px solid #bfdbfe;
                      border-radius:10px; padding:12px 16px; margin:12px 0;
                      font-size:0.85rem; color:#1e40af; }
    .onb-summary-row { display:flex; justify-content:space-between;
                       padding:10px 0; border-bottom:1px solid #f1f5f9; }
    .onb-summary-label { font-size:0.82rem; color:#6b7280; }
    .onb-summary-value { font-size:0.88rem; font-weight:700; color:#1e293b; }

    /* === Header Revenue KPI Cards === */
    .rev-kpi-wrap {
        display:flex; gap:20px; margin:4px 0 12px; align-items:stretch;
    }
    .rev-kpi-card {
        flex:1; background:#ffffff; border:1px solid #e2e8f0;
        border-radius:20px; padding:28px 28px 22px;
        display:flex; flex-direction:column; gap:0;
        box-shadow:0 2px 8px rgba(0,0,0,0.07), 0 0 0 1px rgba(0,0,0,0.03);
        transition:transform .18s ease, box-shadow .18s ease;
    }
    .rev-kpi-card:hover {
        transform:translateY(-3px);
        box-shadow:0 8px 24px rgba(0,0,0,0.12), 0 0 0 1px rgba(0,0,0,0.04);
    }
    .rev-kpi-card.primary {
        flex:1.45;
        background:linear-gradient(145deg,#f0fdf4 0%,#dcfce7 100%);
        border-color:#86efac;
        box-shadow:0 2px 12px rgba(34,197,94,0.15), 0 0 0 1px rgba(134,239,172,0.4);
    }
    .rev-kpi-card.primary:hover {
        box-shadow:0 8px 28px rgba(34,197,94,0.22), 0 0 0 1px rgba(134,239,172,0.5);
    }
    .rev-kpi-card.danger {
        background:linear-gradient(145deg,#fff5f5 0%,#fee2e2 100%);
        border-color:#fca5a5;
        box-shadow:0 2px 12px rgba(239,68,68,0.12), 0 0 0 1px rgba(252,165,165,0.4);
    }
    .rev-kpi-card.danger:hover {
        box-shadow:0 8px 28px rgba(239,68,68,0.18), 0 0 0 1px rgba(252,165,165,0.5);
    }
    .rev-kpi-card.warning {
        background:linear-gradient(145deg,#fffbeb 0%,#fef3c7 100%);
        border-color:#fde68a;
        box-shadow:0 2px 12px rgba(234,179,8,0.12), 0 0 0 1px rgba(253,230,138,0.4);
    }
    .rev-kpi-card.warning:hover {
        box-shadow:0 8px 28px rgba(234,179,8,0.18), 0 0 0 1px rgba(253,230,138,0.5);
    }
    .rev-kpi-card.neutral {
        background:#f8fafc; border-color:#e2e8f0;
    }
    .rev-kpi-icon {
        font-size:1.5rem; line-height:1; margin-bottom:10px;
    }
    .rev-kpi-label {
        font-size:0.68rem; font-weight:700; letter-spacing:.08em;
        text-transform:uppercase; color:#64748b; margin:0 0 6px;
    }
    .rev-kpi-value {
        font-size:2.8rem; font-weight:900; line-height:1;
        letter-spacing:-0.02em; color:#0f172a; margin:0 0 10px;
    }
    .rev-kpi-card.primary  .rev-kpi-value { color:#15803d; }
    .rev-kpi-card.danger   .rev-kpi-value { color:#b91c1c; }
    .rev-kpi-card.warning  .rev-kpi-value { color:#92400e; }
    .rev-kpi-delta {
        font-size:0.73rem; font-weight:600; margin-bottom:8px;
        padding:3px 10px; border-radius:20px; display:inline-block; width:fit-content;
    }
    .rev-kpi-card.primary  .rev-kpi-delta { background:#bbf7d0; color:#166534; }
    .rev-kpi-card.danger   .rev-kpi-delta { background:#fecaca; color:#991b1b; }
    .rev-kpi-card.warning  .rev-kpi-delta { background:#fef08a; color:#713f12; }
    .rev-kpi-card.neutral  .rev-kpi-delta { background:#e2e8f0; color:#475569; }
    .rev-kpi-desc {
        font-size:0.70rem; color:#94a3b8; margin:0; letter-spacing:.01em;
    }

    /* === Smart Pricing Calendar === */
    .cal-grid {
        display:grid; grid-template-columns:repeat(7,1fr); gap:4px; margin:8px 0 12px;
    }
    .cal-cell {
        background:white; border:1px solid #e8e8e8; border-radius:8px;
        padding:6px 4px; text-align:center; min-height:60px;
    }
    .cal-cell-today {
        background:linear-gradient(135deg,#e8e0f8,#d4c5f5);
        border:2px solid #764ba2; border-radius:8px;
        padding:6px 4px; text-align:center; min-height:60px;
    }
    .cal-cell-wknd  { background:#fff8f0; border:1px solid #f39c1233; }
    .cal-cell-event { background:#fff3cd; border:1px solid #ffc10733; }
    .cal-cell-above { background:#fde8e8; border:1px solid #fc818133; }
    .cal-cell-below { background:#e8f5e9; border:1px solid #68d39133; }
    .cal-cell-past  { background:#f5f5f5; border:1px solid #e0e0e0; opacity:0.6; }
    .cal-day-num    { font-size:0.7rem; color:#888; margin-bottom:2px; }
    .cal-day-price  { font-size:0.92rem; font-weight:800; }
    .cal-legend     { display:flex; gap:12px; flex-wrap:wrap;
                      font-size:0.78rem; margin:4px 0 14px; }
    .cal-legend-item{ border-radius:4px; padding:2px 8px; }

    /* === Calendar Interactive Button Grid === */
    /* Scope: horizontal blocks that have at least 7 column children (week rows) */
    div[data-testid="stHorizontalBlock"]:has(
        [data-testid="column"]:nth-child(7)
    ) button[data-testid^="baseButton"] {
        padding: 2px 1px !important;
        min-height: 52px !important;
        font-size: 0.76rem !important;
        white-space: pre-wrap !important;
        line-height: 1.35 !important;
        font-family: inherit !important;
        border-radius: 8px !important;
    }
    /* Remove extra margin from markdown indicator bars inside calendar columns */
    div[data-testid="stHorizontalBlock"]:has(
        [data-testid="column"]:nth-child(7)
    ) div[data-testid="stMarkdownContainer"] > p {
        margin: 0 !important;
        line-height: 1 !important;
    }

    /* ================================================================
       AUTOPILOT STATUS CARD
       ================================================================ */
    .autopilot-card {
        background: linear-gradient(135deg, #1e1b4b 0%, #312e81 50%, #4c1d95 100%);
        border-radius: 20px;
        padding: 28px 32px 24px;
        margin-bottom: 6px;
        box-shadow: 0 8px 32px rgba(99, 102, 241, 0.30);
        position: relative;
        overflow: hidden;
    }
    .autopilot-card::before {
        content: '';
        position: absolute;
        top: -40%;
        right: -5%;
        width: 280px;
        height: 280px;
        background: radial-gradient(circle, rgba(167,139,250,0.18) 0%, transparent 70%);
        pointer-events: none;
    }
    .autopilot-header {
        display: flex;
        align-items: center;
        gap: 12px;
        margin-bottom: 20px;
    }
    .autopilot-dot {
        display: block;
        width: 13px;
        height: 13px;
        background: #4ade80;
        border-radius: 50%;
        flex-shrink: 0;
        animation: ap-pulse 1.8s ease-out infinite;
    }
    @keyframes ap-pulse {
        0%   { box-shadow: 0 0 0 0   rgba(74,222,128,0.75); }
        70%  { box-shadow: 0 0 0 10px rgba(74,222,128,0); }
        100% { box-shadow: 0 0 0 0   rgba(74,222,128,0); }
    }
    .autopilot-title {
        font-size: 1.4rem;
        font-weight: 900;
        color: white;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        line-height: 1;
    }
    .autopilot-subtitle {
        font-size: 0.83rem;
        color: rgba(196,181,253,0.85);
        margin-top: 1px;
    }
    .autopilot-grid {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 12px;
    }
    .autopilot-stat {
        background: rgba(255,255,255,0.09);
        border: 1px solid rgba(255,255,255,0.13);
        border-radius: 12px;
        padding: 14px 16px;
    }
    .autopilot-stat-label {
        font-size: 0.68rem;
        color: rgba(196,181,253,0.80);
        text-transform: uppercase;
        letter-spacing: 0.07em;
        font-weight: 600;
        margin-bottom: 6px;
    }
    .autopilot-stat-value {
        font-size: 1.65rem;
        font-weight: 900;
        color: white;
        line-height: 1;
    }
    .autopilot-stat-sub {
        font-size: 0.62rem;
        color: rgba(196,181,253,0.55);
        margin-top: 4px;
        font-weight: 500;
    }

    /* ================================================================
       SUMMARY METRIC CARDS (3-column row below autopilot)
       ================================================================ */
    .summary-metric-card {
        background: white;
        border: 1px solid #e2e8f0;
        border-radius: 16px;
        padding: 22px 20px 18px;
        text-align: center;
        box-shadow: 0 2px 12px rgba(0,0,0,.06);
        min-height: 120px;
    }
    .sm-icon  { font-size: 1.6rem; margin-bottom: 5px; }
    .sm-label {
        font-size: 0.68rem;
        text-transform: uppercase;
        font-weight: 700;
        color: #94a3b8;
        letter-spacing: 0.06em;
        margin-bottom: 8px;
    }
    .sm-value {
        font-size: 1.9rem;
        font-weight: 900;
        color: #1e293b;
        line-height: 1.1;
        margin-bottom: 4px;
    }
    .sm-sub {
        font-size: 0.75rem;
        color: #94a3b8;
        font-weight: 500;
    }
    .sm-badge {
        display: inline-block;
        border-radius: 8px;
        padding: 4px 12px;
        font-size: 0.82rem;
        font-weight: 700;
        line-height: 1.4;
        margin-bottom: 4px;
    }

    /* ================================================================
       DECISION FEED CARDS
       ================================================================ */
    .dec-card {
        background: white;
        border-radius: 14px;
        border: 1px solid #e2e8f0;
        border-left: 4px solid #e2e8f0;
        padding: 18px 20px 16px;
        margin-bottom: 12px;
        box-shadow: 0 2px 8px rgba(0,0,0,.05);
    }
    .dec-card-up   { border-left-color: #16a34a; }
    .dec-card-down { border-left-color: #dc2626; }
    .dec-card-flat { border-left-color: #6b7280; }

    .dec-prop-name {
        font-size: 1.0rem;
        font-weight: 800;
        color: #1e293b;
    }
    .dec-action-tag {
        display: inline-block;
        font-size: 0.68rem;
        font-weight: 700;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        border-radius: 20px;
        padding: 2px 9px;
        margin-bottom: 12px;
        margin-top: 4px;
    }
    .dec-action-up   { background: #dcfce7; color: #166534; }
    .dec-action-down { background: #fee2e2; color: #991b1b; }
    .dec-action-flat { background: #f3f4f6; color: #6b7280; }

    .dec-prices {
        display: flex;
        align-items: baseline;
        gap: 7px;
        margin-bottom: 14px;
        flex-wrap: wrap;
    }
    .dec-price-old {
        font-size: 1.05rem;
        color: #94a3b8;
        text-decoration: line-through;
    }
    .dec-price-sep { font-size: 1.1rem; color: #cbd5e1; }
    .dec-price-new {
        font-size: 1.6rem;
        font-weight: 900;
        line-height: 1;
    }
    .dec-price-new-up   { color: #16a34a; }
    .dec-price-new-down { color: #dc2626; }
    .dec-price-new-flat { color: #334155; }
    .dec-pct-badge {
        font-size: 0.75rem;
        font-weight: 700;
        border-radius: 6px;
        padding: 2px 7px;
    }

    .dec-reasons-hdr {
        font-size: 0.66rem;
        text-transform: uppercase;
        font-weight: 700;
        color: #94a3b8;
        letter-spacing: 0.07em;
        margin-bottom: 7px;
    }
    .dec-reason {
        display: flex;
        align-items: flex-start;
        gap: 8px;
        font-size: 0.87rem;
        color: #374151;
        margin-bottom: 5px;
        line-height: 1.4;
    }
    .dec-reason-icon { flex-shrink: 0; font-size: 0.92rem; margin-top: 1px; }

    .dec-footer {
        font-size: 0.72rem;
        color: #94a3b8;
        margin-top: 12px;
        padding-top: 10px;
        border-top: 1px solid #f1f5f9;
        display: flex;
        justify-content: space-between;
        align-items: center;
    }

    /* ================================================================
       PRICING STRATEGY PAGE
       ================================================================ */
    .ps-section-title {
        font-size: 0.78rem;
        font-weight: 800;
        text-transform: uppercase;
        letter-spacing: 0.07em;
        color: #64748b;
        margin-bottom: 4px;
    }
    .ps-section-desc {
        font-size: 0.85rem;
        color: #6b7280;
        line-height: 1.5;
        margin-bottom: 16px;
    }
    .strat-card {
        border-radius: 14px;
        padding: 20px 16px 18px;
        text-align: center;
        cursor: pointer;
        transition: box-shadow .15s;
        margin-bottom: 6px;
        min-height: 130px;
    }
    .strat-card:hover {
        box-shadow: 0 4px 16px rgba(102,126,234,.15);
    }
    .ps-guardrail-note {
        font-size: 0.76rem;
        color: #94a3b8;
        margin-top: 6px;
        line-height: 1.45;
    }
    .ps-save-hint {
        background: #f0fdf4;
        border: 1px solid #bbf7d0;
        border-radius: 10px;
        padding: 10px 16px;
        font-size: 0.82rem;
        color: #166534;
        margin-top: 12px;
    }

    /* Placeholder card when feed is empty */
    .dec-empty {
        background: #f8fafc;
        border: 2px dashed #e2e8f0;
        border-radius: 14px;
        padding: 36px 24px;
        text-align: center;
    }

    /* ================================================================
       MARKET INTELLIGENCE PANEL — "Contesto di Mercato"
       ================================================================ */
    .mkt-metric {
        background: white;
        border: 1px solid #e2e8f0;
        border-radius: 13px;
        padding: 16px 14px 14px;
        text-align: center;
        box-shadow: 0 1px 6px rgba(0,0,0,.05);
        height: 100%;
    }
    /* Highlight border on the "Tuo prezzo" card */
    .mkt-metric-yours {
        border-width: 2px;
    }
    .mkt-metric-icon  { font-size: 1.25rem; margin-bottom: 5px; }
    .mkt-metric-label {
        font-size: 0.64rem;
        text-transform: uppercase;
        font-weight: 700;
        color: #94a3b8;
        letter-spacing: 0.07em;
        margin-bottom: 6px;
    }
    .mkt-metric-value {
        font-size: 1.5rem;
        font-weight: 900;
        color: #1e293b;
        line-height: 1;
    }
    .mkt-metric-sub {
        font-size: 0.7rem;
        color: #94a3b8;
        margin-top: 4px;
    }

    /* Position + explanation bar */
    .mkt-context-bar {
        background: white;
        border: 1px solid #e2e8f0;
        border-radius: 13px;
        padding: 14px 20px;
        margin-top: 10px;
        display: flex;
        align-items: center;
        gap: 16px;
        box-shadow: 0 1px 6px rgba(0,0,0,.05);
        flex-wrap: wrap;
    }
    .mkt-position-lbl {
        font-size: 0.68rem;
        text-transform: uppercase;
        font-weight: 700;
        color: #94a3b8;
        letter-spacing: 0.06em;
        white-space: nowrap;
    }
    .mkt-pos-badge {
        display: inline-block;
        font-size: 0.82rem;
        font-weight: 700;
        border-radius: 20px;
        padding: 4px 14px;
        white-space: nowrap;
    }
    .mkt-explanation {
        font-size: 0.80rem;
        color: #6b7280;
        line-height: 1.5;
        flex: 1;
        min-width: 180px;
    }

    /* Visual position bar — percentage strip */
    .mkt-bar-track {
        background: #f1f5f9;
        border-radius: 99px;
        height: 6px;
        margin: 10px 0 4px;
        position: relative;
        overflow: visible;
    }
    .mkt-bar-range {
        background: linear-gradient(90deg, #bfdbfe, #93c5fd);
        border-radius: 99px;
        height: 6px;
        position: absolute;
    }
    .mkt-bar-dot {
        width: 14px;
        height: 14px;
        border-radius: 50%;
        border: 2px solid white;
        position: absolute;
        top: -4px;
        transform: translateX(-50%);
        box-shadow: 0 1px 4px rgba(0,0,0,.2);
    }
</style>
""", unsafe_allow_html=True)


# ═════════════════════════════════════════════════════════════════════════════
# SIDEBAR – CONFIGURAZIONE
# ═════════════════════════════════════════════════════════════════════════════

def render_sidebar():
    """
    Sidebar SaaS-friendly:
    - Selettore proprietà attiva (sessione)
    - Strategia pricing
    - Soglie occupancy
    - NESSUN campo tecnico Telegram (gestito nella tab dedicata)
    """
    cfg = load_config()

    with st.sidebar:
        st.markdown(
            "<div style='display:flex;align-items:center;gap:10px;margin-bottom:4px'>"
            "<span style='font-size:1.8rem'>✈️</span>"
            "<span style='font-size:1.3rem;font-weight:800;color:#1a1a2e'>PricePilot</span>"
            "</div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            "<small style='color:#888'>Dynamic Pricing · Affitti Brevi</small>",
            unsafe_allow_html=True,
        )
        st.markdown("---")

        # ── Selettore proprietà attiva ────────────────────────────────────────
        account_id = current_account_id()
        props = list_properties(account_id=account_id)
        active_prop = None
        if props:
            st.markdown("### 🏠 Proprietà attiva")
            prop_options = {p["id"]: p["name"] for p in props}
            default_id   = st.session_state.get("active_prop_id", props[0]["id"])
            if default_id not in prop_options:
                default_id = props[0]["id"]
            if st.session_state.get("sidebar_prop_sel") != default_id:
                st.session_state["sidebar_prop_sel"] = default_id

            active_id = st.selectbox(
                "Seleziona proprietà",
                options=list(prop_options.keys()),
                format_func=lambda x: prop_options[x],
                index=list(prop_options.keys()).index(default_id),
                key="sidebar_prop_sel",
                on_change=_sync_active_property_from_sidebar,
                label_visibility="collapsed",
            )
            st.session_state["active_prop_id"] = active_id

            # Badge modalità
            active_prop = next((p for p in props if p["id"] == active_id), None)
            if active_prop:
                mode = active_prop.get("sync_mode", "advisory")
                mode_colors = {
                    "advisory": "#667eea", "approval": "#f39c12", "auto": "#27ae60"
                }
                mode_labels = {
                    "advisory": "💡 Manuale", "approval": "✅ Approvazione", "auto": "🤖 Automatico"
                }
                st.markdown(
                    f"<span style='background:{mode_colors.get(mode,'#999')};"
                    f"color:white;padding:3px 10px;border-radius:20px;font-size:0.8rem;"
                    f"font-weight:600'>{mode_labels.get(mode, mode.upper())}</span>",
                    unsafe_allow_html=True,
                )
                # Stato Telegram
                try:
                    from pricepilot.core.database import get_telegram_link_by_property
                    tg_link = get_telegram_link_by_property(active_id)
                    if tg_link and tg_link.get("chat_id"):
                        uname = tg_link.get("telegram_username", "")
                        st.markdown(
                            f"<small style='color:#27ae60'>🔔 Telegram: @{uname or 'collegato'}</small>",
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(
                            "<small style='color:#e74c3c'>🔕 Telegram: non collegato</small>",
                            unsafe_allow_html=True,
                        )
                except Exception:
                    pass
        else:
            st.info("Nessuna proprietà. Crea la prima nella tab 🏠 Proprietà.")
            st.session_state.setdefault("active_prop_id", None)

        st.markdown("---")
        if active_prop:
            apply_pending_property_pricing_widget_reset(active_prop["id"], sidebar=True)

        # ── Configurazione strategia ──────────────────────────────────────────
        st.markdown("### 🎯 Strategia")
        _STRATEGY_TAGS = {
            "conservative": "📥 Più prenotazioni · stabilità sopra tutto",
            "balanced":     "⚖️ Bilanciato · revenue e occupazione",
            "aggressive":   "💰 Più revenue · massimizza nelle date ad alta domanda",
            "premium":      "💎 Premium · prezzi elevati, meno sensibile ai competitor",
        }
        strategy_options = list(STRATEGIES.keys())
        sidebar_strategy = active_prop.get("strategy", cfg.get("strategy", "balanced")) if active_prop else cfg.get("strategy", "balanced")
        if sidebar_strategy not in strategy_options:
            sidebar_strategy = "balanced"
        current_idx = strategy_options.index(sidebar_strategy)
        strategy_sel = st.selectbox(
            "Strategia pricing",
            options=strategy_options,
            format_func=lambda k: STRATEGIES[k].label,
            index=current_idx,
            key=f"sidebar_strategy_{active_prop['id'] if active_prop else 'global'}",
            help=(
                "Determina come PricePilot bilancia occupazione e ricavo. "
                "Conservativa = più stabilità e prenotazioni. "
                "Premium = prezzi sempre elevati, meno sensibile ai competitor."
            ),
        )
        st.caption(
            f"{_STRATEGY_TAGS.get(strategy_sel, '')} — "
            f"{STRATEGIES[strategy_sel].description}"
        )

        st.markdown("### 💰 Prezzi")
        if active_prop:
            st.caption("Stessi limiti della tab Prezzi per la proprieta attiva.")
        else:
            st.caption("Crea una proprieta per salvare limiti specifici.")
        sidebar_min_value, sidebar_max_value = get_synced_price_limits(active_prop, cfg)
        if active_prop:
            _sidebar_flash = st.session_state.pop(f"pp_sidebar_price_flash_{active_prop['id']}", None)
        else:
            _sidebar_flash = st.session_state.pop("pp_sidebar_price_flash_global", None)
        if _sidebar_flash:
            st.success(_sidebar_flash)
        sidebar_price_key_suffix = _price_limit_widget_suffix(active_prop, cfg)
        col1, col2 = st.columns(2)
        with col1:
            min_price = st.number_input(
                "Min (€)", min_value=10.0, max_value=9990.0,
                value=sidebar_min_value, step=5.0,
                key=f"sidebar_min_price_{sidebar_price_key_suffix}",
                help=(
                    "Prezzo minimo assoluto per notte. "
                    "PricePilot non scenderà mai sotto questa soglia. "
                    "Impostalo vicino al tuo costo operativo per notte."
                ),
            )
        with col2:
            max_price = st.number_input(
                "Max (€)", min_value=50.0, max_value=9999.0,
                value=sidebar_max_value, step=10.0,
                key=f"sidebar_max_price_{sidebar_price_key_suffix}",
                help=(
                    "Prezzo massimo assoluto per notte. "
                    "PricePilot non supererà mai questo valore, "
                    "evitando picchi di prezzo irragionevoli."
                ),
            )
        max_change = st.slider("Variazione max (%)", 5, 50,
                               int(float(cfg.get("max_change_pct", 0.20)) * 100)) / 100

        st.markdown("### 📊 Occupazione")
        occ_low  = st.slider(
            "Soglia bassa", 0.10, 0.50,
            float(cfg.get("occupancy_low_threshold", 0.30)),
            help=(
                "Se la domanda stimata scende sotto questa soglia, "
                "PricePilot può abbassare il prezzo per favorire le prenotazioni."
            ),
        )
        occ_high = st.slider(
            "Soglia alta", 0.50, 0.95,
            float(cfg.get("occupancy_high_threshold", 0.80)),
            help=(
                "Se la domanda supera questa soglia, "
                "PricePilot può aumentare il prezzo più aggressivamente "
                "per massimizzare il ricavo per notte."
            ),
        )

        if st.button("💾 Salva configurazione", use_container_width=True):
            if float(min_price) >= float(max_price):
                st.error("Il prezzo minimo deve essere inferiore al prezzo massimo.")
                return load_config()
            new_cfg = {
                **cfg,
                "min_price_per_night":      min_price,
                "max_price_per_night":      max_price,
                "max_change_pct":           max_change,
                "strategy":                 strategy_sel,
                "occupancy_low_threshold":  occ_low,
                "occupancy_high_threshold": occ_high,
            }
            if active_prop:
                prop_id = active_prop["id"]
                save_synced_price_limits(
                    active_prop,
                    float(min_price),
                    float(max_price),
                    strategy=strategy_sel,
                    reset_sidebar=False,
                    reset_pricing_tab=True,
                    pricing_rules={
                        "max_change_pct": max_change,
                        "occupancy_low_threshold": occ_low,
                        "occupancy_high_threshold": occ_high,
                    },
                )
                save_config({
                    **load_config(),
                    "min_price_per_night": float(min_price),
                    "max_price_per_night": float(max_price),
                    "max_change_pct": max_change,
                    "strategy": strategy_sel,
                    "occupancy_low_threshold": occ_low,
                    "occupancy_high_threshold": occ_high,
                })
                st.session_state[f"pp_sidebar_price_flash_{prop_id}"] = (
                    "Limiti prezzo salvati e sincronizzati."
                )
            else:
                save_config(new_cfg)
                st.cache_data.clear()
                st.session_state["pp_sidebar_price_flash_global"] = "Configurazione salvata."
            st.rerun()

        st.markdown("---")
        st.markdown(
            "<small style='color:#aaa'>PricePilot v4.0 · SaaS Edition</small>",
            unsafe_allow_html=True,
        )

    return load_config()


# =============================================================================
# TAB: HOME OVERVIEW
# =============================================================================

def _tab_onboarding():
    """
    Wizard di onboarding a 4 step per nuovi utenti (nessuna proprietà configurata).
    Gestisce tutto lo stato via st.session_state con prefisso 'onb_'.
    Non modifica il pricing engine né il database schema.
    """
    return _tab_onboarding_v2("legacy")
    import re as _re
    import secrets as _sec
    account_id = current_account_id()

    # ── State keys ──────────────────────────────────────────────────────────
    _step_key  = "onb_step"
    _url_key   = "onb_url"
    _name_key  = "onb_name"
    _city_key  = "onb_city"
    _plat_key  = "onb_platform"
    _min_key   = "onb_min_price"
    _max_key   = "onb_max_price"
    _tglink_key= "onb_tg_link"

    if _step_key not in st.session_state:
        st.session_state[_step_key] = 1

    step = st.session_state[_step_key]

    # ── Progress bar ────────────────────────────────────────────────────────
    pct = {1: 25, 2: 50, 3: 75, 4: 100}[step]
    step_labels = {1: "Collega proprietà", 2: "Prezzi", 3: "Notifiche", 4: "Completato"}

    # outer centering column
    _, _mid, _ = st.columns([1, 2, 1])
    with _mid:
        # Step indicator dots
        dots_html = "".join(
            f'<div style="width:28px;height:28px;border-radius:50%;display:flex;'
            f'align-items:center;justify-content:center;font-size:0.75rem;font-weight:800;'
            f'background:{"linear-gradient(135deg,#667eea,#764ba2)" if i <= step else "#e8ecff"};'
            f'color:{"white" if i <= step else "#9ca3af"}">{i}</div>'
            for i in range(1, 5)
        )
        labels_html = "".join(
            f'<div style="font-size:0.68rem;color:{"#667eea" if i == step else "#9ca3af"};'
            f'font-weight:{"700" if i == step else "400"};text-align:center;'
            f'white-space:nowrap">{step_labels[i]}</div>'
            for i in range(1, 5)
        )
        st.markdown(
            f'<div style="margin:28px 0 8px">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'gap:8px;margin-bottom:6px">{dots_html}</div>'
            f'<div class="onb-progress-track"><div class="onb-progress-fill" '
            f'style="width:{pct}%"></div></div>'
            f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:4px;'
            f'margin-top:4px">{labels_html}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # ── STEP 1: Collega il tuo appartamento ────────────────────────────
        if step == 1:
            st.markdown(
                '<div class="onb-step-label">Passo 1 di 4</div>'
                '<div class="onb-title">Collega il tuo appartamento</div>'
                '<div class="onb-desc">PricePilot analizza automaticamente il mercato '
                'per suggerire il prezzo ottimale.</div>',
                unsafe_allow_html=True,
            )

            url_input = st.text_input(
                "Link Airbnb o Booking",
                value=st.session_state.get(_url_key, ""),
                placeholder="https://airbnb.com/rooms/123456",
                key="onb_url_input",
                label_visibility="visible",
            )

            # Detect platform from URL
            def _detect_platform(u: str) -> str:
                u = u.lower()
                if "airbnb" in u:    return "airbnb"
                if "booking.com" in u: return "booking"
                if "vrbo" in u:      return "vrbo"
                return "other"

            if url_input and url_input.strip():
                _plat = _detect_platform(url_input)
                _suggested_name = _suggest_name_from_url(url_input, _plat)

                if _suggested_name:
                    st.markdown(
                        '<div class="onb-found-card">'
                        '<div style="font-size:0.8rem;font-weight:700;color:#16a34a;'
                        'margin-bottom:6px">✅ Abbiamo trovato:</div>'
                        '</div>',
                        unsafe_allow_html=True,
                    )
                    # Editable name
                    _name_val = st.text_input(
                        "Nome proprietà",
                        value=st.session_state.get(_name_key, _suggested_name),
                        key="onb_name_confirm",
                    )
                    _city_val = st.text_input(
                        "Città",
                        value=st.session_state.get(_city_key, ""),
                        placeholder="es. Milano",
                        key="onb_city_confirm",
                    )

                    _b1, _b2 = st.columns([1, 1])
                    with _b1:
                        if st.button("✅ Conferma proprietà", type="primary",
                                     use_container_width=True, key="onb_confirm"):
                            if not _name_val.strip():
                                st.error("Inserisci il nome della proprietà.")
                            else:
                                st.session_state[_url_key]  = url_input.strip()
                                st.session_state[_name_key] = _name_val.strip()
                                st.session_state[_city_key] = _city_val.strip()
                                st.session_state[_plat_key] = _plat
                                st.session_state[_step_key] = 2
                                st.rerun()
                    with _b2:
                        if st.button("✏️ Modifica", use_container_width=True,
                                     key="onb_edit"):
                            st.session_state[_url_key] = ""
                            st.rerun()
                else:
                    # URL valid but no name extracted — ask manually
                    st.markdown(
                        '<div class="onb-found-card" style="background:#fef9c3;'
                        'border-color:#fde68a">'
                        '<div style="font-size:0.8rem;font-weight:700;color:#92400e;'
                        'margin-bottom:6px">⚠️ Inserisci i dettagli manualmente:</div>'
                        '</div>',
                        unsafe_allow_html=True,
                    )
                    _name_val = st.text_input(
                        "Nome proprietà",
                        value=st.session_state.get(_name_key, ""),
                        placeholder="es. Appartamento Centro Milano",
                        key="onb_name_manual",
                    )
                    _city_val = st.text_input(
                        "Città",
                        value=st.session_state.get(_city_key, ""),
                        placeholder="es. Milano",
                        key="onb_city_manual",
                    )

                    if st.button("Avanti →", type="primary",
                                 use_container_width=True, key="onb_manual_next"):
                        if not _name_val.strip():
                            st.error("Inserisci il nome della proprietà.")
                        else:
                            st.session_state[_url_key]  = url_input.strip()
                            st.session_state[_name_key] = _name_val.strip()
                            st.session_state[_city_key] = _city_val.strip()
                            st.session_state[_plat_key] = _detect_platform(url_input)
                            st.session_state[_step_key] = 2
                            st.rerun()
            else:
                # No URL yet — show prompt
                st.markdown(
                    '<div style="background:#f8fafc;border-radius:10px;padding:14px 18px;'
                    'color:#94a3b8;font-size:0.88rem;text-align:center;margin-top:8px">'
                    '📋 Incolla il link del tuo annuncio Airbnb o Booking per iniziare'
                    '</div>',
                    unsafe_allow_html=True,
                )

        # ── STEP 2: Imposta i limiti di prezzo ─────────────────────────────
        elif step == 2:
            _prop_name = st.session_state.get(_name_key, "")
            _prop_city = st.session_state.get(_city_key, "")

            st.markdown(
                '<div class="onb-step-label">Passo 2 di 4</div>'
                '<div class="onb-title">Imposta i limiti di prezzo</div>'
                '<div class="onb-desc">PricePilot gestirà automaticamente i prezzi '
                'restando dentro questi limiti.</div>',
                unsafe_allow_html=True,
            )

            # Market suggestion using engine
            _base_ref = 100.0
            try:
                from pricepilot.engine.market_analyzer import simulate_competitors, calculate_market_stats
                from pricepilot.data_sources.events import get_event_for_date, event_to_string
                _evt = get_event_for_date(date.today())
                _evt_s = event_to_string(_evt)
                _comps_onb = simulate_competitors(_base_ref, date.today(), _evt_s, 6)
                _mstats_onb = calculate_market_stats(_comps_onb)
                _mkt_avg_onb = round(_mstats_onb["market_avg"])
            except Exception:
                _mkt_avg_onb = 145

            st.markdown(
                f'<div class="onb-suggestion">'
                f'💡 Prezzo medio nella tua zona: <strong>€{_mkt_avg_onb}</strong>'
                f' — puoi impostare i limiti attorno a questo valore.'
                f'</div>',
                unsafe_allow_html=True,
            )

            _min_default = max(30, round(_mkt_avg_onb * 0.55))
            _max_default = round(_mkt_avg_onb * 1.45)

            _pc1, _pc2 = st.columns(2)
            with _pc1:
                _min_val = st.number_input(
                    "💰 Prezzo minimo (€)",
                    min_value=10, max_value=2000,
                    value=st.session_state.get(_min_key, _min_default),
                    step=5, key="onb_min_input",
                )
            with _pc2:
                _max_val = st.number_input(
                    "💰 Prezzo massimo (€)",
                    min_value=11, max_value=5000,
                    value=st.session_state.get(_max_key, _max_default),
                    step=5, key="onb_max_input",
                )

            _bk1, _bk2 = st.columns([1, 1])
            with _bk1:
                if st.button("← Indietro", use_container_width=True, key="onb_back2"):
                    st.session_state[_step_key] = 1
                    st.rerun()
            with _bk2:
                if st.button("🚀 Attiva autopilot", type="primary",
                             use_container_width=True, key="onb_activate"):
                    if _min_val >= _max_val:
                        st.error("Il prezzo minimo deve essere inferiore al massimo.")
                    else:
                        # Create property in DB
                        try:
                            _new_prop = create_property({
                                "account_id": account_id,
                                "name":      _prop_name,
                                "city":      _prop_city,
                                "platform":  st.session_state.get(_plat_key, "airbnb"),
                                "listing_url": st.session_state.get(_url_key, ""),
                                "min_price": float(_min_val),
                                "max_price": float(_max_val),
                                "sync_mode": "auto",
                                "strategy":  "balanced",
                            })
                            st.session_state["active_prop_id"] = _new_prop["id"]
                            _remember_saved_price_limits(_new_prop["id"], _min_val, _max_val)
                            queue_property_pricing_widget_reset(
                                _new_prop["id"],
                                sidebar=True,
                                pricing_tab=True,
                            )
                            st.session_state[_min_key] = _min_val
                            st.session_state[_max_key] = _max_val
                            st.session_state[_step_key] = 3
                            st.rerun()
                        except Exception as _e:
                            st.error(f"Errore durante la creazione: {_e}")

        # ── STEP 3: Connetti Telegram ───────────────────────────────────────
        elif step == 3:
            st.markdown(
                '<div class="onb-step-label">Passo 3 di 4</div>'
                '<div class="onb-title">Ricevi notifiche sui cambi di prezzo</div>'
                '<div class="onb-desc">PricePilot può inviarti un messaggio Telegram '
                'ogni volta che aggiorna i prezzi o richiede la tua approvazione.</div>',
                unsafe_allow_html=True,
            )

            _pid = st.session_state.get("active_prop_id")
            _existing_link = st.session_state.get(_tglink_key)

            if not _existing_link:
                if st.button("📱 Connetti Telegram", type="primary",
                             use_container_width=True, key="onb_tg_connect"):
                    with st.spinner("Generazione link…"):
                        try:
                            _token = f"connect_{_pid}_{_sec.token_hex(8)}"
                            revoke_telegram_link(_pid)
                            _lid = save_telegram_link({
                                "property_id": _pid,
                                "token":       _token,
                                "active":      1,
                            })
                            _bot_user = os.environ.get(
                                "TELEGRAM_BOT_USERNAME", "PricePilotBot"
                            ).strip().lstrip("@")
                            _link_data = {
                                "link_id":   _lid,
                                "token":     _token,
                                "deep_link": f"https://t.me/{_bot_user}?start={_token}",
                            }
                            st.session_state[_tglink_key] = _link_data
                            st.rerun()
                        except Exception as _e:
                            st.error(f"Errore: {_e}")
            else:
                _deep = _existing_link.get("deep_link", "")
                st.markdown(
                    f'<div style="background:#f0fdf4;border:1.5px solid #86efac;'
                    f'border-radius:12px;padding:18px 20px;margin-bottom:16px">'
                    f'<div style="font-size:0.82rem;font-weight:700;color:#16a34a;'
                    f'margin-bottom:6px">✅ Link generato</div>'
                    f'<div style="font-size:0.82rem;color:#374151;margin-bottom:12px">'
                    f'Clicca il pulsante qui sotto per aprire Telegram e avviare il bot:</div>'
                    f'<a href="{_deep}" target="_blank" '
                    f'style="background:linear-gradient(135deg,#229ED9,#1a8fc1);'
                    f'color:white;padding:10px 22px;border-radius:10px;'
                    f'font-weight:700;font-size:0.88rem;text-decoration:none;'
                    f'display:inline-block">✈️ Apri Telegram</a>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            _bt1, _bt2 = st.columns([1, 1])
            with _bt1:
                if st.button("← Indietro", use_container_width=True, key="onb_back3"):
                    st.session_state[_step_key] = 2
                    st.rerun()
            with _bt2:
                _skip_label = "Salta per ora →" if not _existing_link else "Avanti →"
                if st.button(_skip_label, use_container_width=True, key="onb_skip3",
                             type="secondary" if not _existing_link else "primary"):
                    st.session_state[_step_key] = 4
                    st.rerun()

        # ── STEP 4: Autopilot attivo ────────────────────────────────────────
        elif step == 4:
            _prop_name = st.session_state.get(_name_key, "")
            _prop_city = st.session_state.get(_city_key, "—")
            _min_p     = st.session_state.get(_min_key, 0)
            _max_p     = st.session_state.get(_max_key, 0)

            st.markdown(
                '<div style="text-align:center;padding:8px 0 20px">'
                '<div style="font-size:3.5rem;margin-bottom:8px">🚀</div>'
                '<div style="font-size:1.65rem;font-weight:900;color:#1a1a2e;'
                'margin-bottom:6px">Autopilot attivo ✔</div>'
                '<div style="font-size:0.95rem;color:#6b7280;line-height:1.7">'
                'PricePilot analizzerà il mercato ogni 6 ore<br>'
                'per ottimizzare automaticamente i tuoi prezzi.'
                '</div>'
                '</div>',
                unsafe_allow_html=True,
            )

            _pname_esc = _html.escape(_prop_name)
            _pcity_esc = _html.escape(_prop_city)
            _plat_esc  = _html.escape(
                {"airbnb": "Airbnb", "booking": "Booking.com",
                 "vrbo": "Vrbo", "other": "Diretto"}.get(
                    st.session_state.get(_plat_key, ""), "—"
                )
            )

            st.markdown(
                f'<div style="background:#f8fafc;border:1px solid #e2e8f0;'
                f'border-radius:14px;padding:20px 24px;margin:16px 0 24px">'
                f'<div style="font-size:0.72rem;font-weight:700;color:#94a3b8;'
                f'text-transform:uppercase;letter-spacing:.06em;margin-bottom:12px">'
                f'Riepilogo configurazione</div>'
                f'<div class="onb-summary-row">'
                f'<span class="onb-summary-label">🏠 Nome proprietà</span>'
                f'<span class="onb-summary-value">{_pname_esc}</span></div>'
                f'<div class="onb-summary-row">'
                f'<span class="onb-summary-label">📍 Città</span>'
                f'<span class="onb-summary-value">{_pcity_esc}</span></div>'
                f'<div class="onb-summary-row">'
                f'<span class="onb-summary-label">🌐 Piattaforma</span>'
                f'<span class="onb-summary-value">{_plat_esc}</span></div>'
                f'<div class="onb-summary-row">'
                f'<span class="onb-summary-label">💰 Prezzo minimo</span>'
                f'<span class="onb-summary-value">€{_min_p}</span></div>'
                f'<div class="onb-summary-row" style="border-bottom:none">'
                f'<span class="onb-summary-label">💰 Prezzo massimo</span>'
                f'<span class="onb-summary-value">€{_max_p}</span></div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            if st.button("🏠 Vai alla dashboard", type="primary",
                         use_container_width=True, key="onb_finish"):
                # Clear onboarding state
                for _k in [_step_key, _url_key, _name_key, _city_key,
                           _plat_key, _min_key, _max_key, _tglink_key,
                           "onb_url_input", "onb_name_confirm", "onb_city_confirm",
                           "onb_name_manual", "onb_city_manual"]:
                    st.session_state.pop(_k, None)
                st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
def _onb_detect_platform(url: str) -> str:
    url = (url or "").lower()
    if "airbnb" in url:
        return "airbnb"
    if "booking.com" in url:
        return "booking"
    if "vrbo" in url:
        return "vrbo"
    if url:
        return "other"
    return "airbnb"


def _onb_platform_label(platform: str) -> str:
    return {
        "airbnb": "Airbnb",
        "booking": "Booking.com",
        "vrbo": "Vrbo",
        "direct": "Sito diretto",
        "other": "Altro",
    }.get(platform, str(platform or "Altro").title())


def _plan_action_copy(plan: str) -> tuple[str, str, str]:
    plan = (plan or "free").lower()
    if plan == "plus":
        return (
            "Plus",
            "PricePilot invia proposta e motivazione su Telegram. L'utente approva, poi la sync OTA sara gestita dal channel manager.",
            "Approvazione Telegram",
        )
    if plan == "pro":
        return (
            "Pro",
            "PricePilot lavora in autonomia con guardrail, report e notifiche operative. La sync reale arrivera con channel manager/API.",
            "Autopilot",
        )
    return (
        "Free",
        "PricePilot invia consigli motivati su Telegram. L'utente aggiorna manualmente i prezzi sulle OTA.",
        "Consigli motivati",
    )


def _render_readonly_plan_box(plan: str, *, compact: bool = False):
    label, desc, mode = _plan_action_copy(plan)
    padding = "10px 12px" if compact else "14px 16px"
    st.markdown(
        f'<div style="background:#f8fafc;border:1px solid #e2e8f0;'
        f'border-radius:10px;padding:{padding};margin:2px 0 8px">'
        f'<div style="font-size:0.72rem;font-weight:800;color:#64748b;'
        f'text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">'
        f'Piano attivo</div>'
        f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
        f'<span style="font-size:1.05rem;font-weight:900;color:#111827">{label}</span>'
        f'<span style="background:#e0f2fe;color:#0369a1;border-radius:999px;'
        f'font-size:0.72rem;font-weight:800;padding:2px 8px">{mode}</span>'
        f'</div>'
        f'<div style="font-size:0.82rem;color:#64748b;margin-top:6px;line-height:1.45">'
        f'{desc}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def _clear_onboarding_state():
    for key in list(st.session_state.keys()):
        if str(key).startswith("onb_"):
            st.session_state.pop(key, None)


def _tab_onboarding_v2(surface: str = "main"):
    account_id = current_account_id()
    account = get_account(account_id) or {
        "id": account_id,
        "name": "La mia attivita",
        "plan": "free",
        "billing_status": "dev",
    }
    widget_key = lambda name: f"{name}_{surface}"

    step_key = "onb_step"
    st.session_state.setdefault(step_key, 1)
    step = int(st.session_state.get(step_key) or 1)
    step = min(max(step, 1), 4)
    st.session_state[step_key] = step

    labels = {
        1: "Attivita",
        2: "Proprieta",
        3: "Piano",
        4: "Telegram",
    }
    pct = {1: 25, 2: 50, 3: 75, 4: 100}[step]

    _, mid, _ = st.columns([1, 2.2, 1])
    with mid:
        dots_html = "".join(
            f'<div style="width:28px;height:28px;border-radius:50%;display:flex;'
            f'align-items:center;justify-content:center;font-size:0.75rem;font-weight:800;'
            f'background:{"#2563eb" if i <= step else "#e5e7eb"};'
            f'color:{"white" if i <= step else "#9ca3af"}">{i}</div>'
            for i in range(1, 5)
        )
        labels_html = "".join(
            f'<div style="font-size:0.70rem;color:{"#2563eb" if i == step else "#9ca3af"};'
            f'font-weight:{"800" if i == step else "500"};text-align:center;'
            f'white-space:nowrap">{labels[i]}</div>'
            for i in range(1, 5)
        )
        st.markdown(
            f'<div style="margin:14px 0 18px">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'gap:8px;margin-bottom:6px">{dots_html}</div>'
            f'<div class="onb-progress-track" style="margin-bottom:6px">'
            f'<div class="onb-progress-fill" style="width:{pct}%"></div></div>'
            f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:4px">'
            f'{labels_html}</div></div>',
            unsafe_allow_html=True,
        )

        if step == 1:
            st.markdown(
                '<div class="onb-step-label">Passo 1 di 4</div>'
                '<div class="onb-title">Partiamo dalla tua attivita</div>'
                '<div class="onb-desc">Questi dati servono per intestare il tuo account '
                'e creare la prima proprieta da monitorare.</div>',
                unsafe_allow_html=True,
            )

            business_name = st.text_input(
                "Nome attivita",
                value=st.session_state.get("onb_business_name", account.get("name") or "La mia attivita"),
                placeholder="Es. Rossi Apartments",
                key=widget_key("onb_business_name_input"),
            )
            property_name = st.text_input(
                "Nome prima proprieta",
                value=st.session_state.get("onb_property_name", ""),
                placeholder="Es. Appartamento Centro Roma",
                key=widget_key("onb_property_name_input"),
            )
            city_zone = st.text_input(
                "Citta / zona",
                value=st.session_state.get("onb_city_zone", ""),
                placeholder="Es. Roma, Trastevere",
                key=widget_key("onb_city_zone_input"),
            )

            if st.button("Avanti", type="primary", use_container_width=True, key=widget_key("onb_step1_next")):
                if not business_name.strip():
                    st.error("Inserisci il nome della tua attivita.")
                elif not property_name.strip():
                    st.error("Inserisci il nome della prima proprieta.")
                elif not city_zone.strip():
                    st.error("Inserisci almeno citta o zona.")
                else:
                    st.session_state["onb_business_name"] = business_name.strip()
                    st.session_state["onb_property_name"] = property_name.strip()
                    st.session_state["onb_city_zone"] = city_zone.strip()
                    st.session_state[step_key] = 2
                    st.rerun()

        elif step == 2:
            st.markdown(
                '<div class="onb-step-label">Passo 2 di 4</div>'
                '<div class="onb-title">Canali e prezzi base</div>'
                '<div class="onb-desc">PricePilot usa questi limiti come guardrail: '
                'il prezzo consigliato non uscira da questa fascia.</div>',
                unsafe_allow_html=True,
            )

            platform_options = ["airbnb", "booking", "vrbo", "direct", "other"]
            saved_platforms = st.session_state.get("onb_platforms") or ["airbnb"]
            platforms = st.multiselect(
                "Piattaforme usate",
                options=platform_options,
                default=[p for p in saved_platforms if p in platform_options] or ["airbnb"],
                format_func=_onb_platform_label,
                key=widget_key("onb_platforms_input"),
            )
            listing_url = st.text_input(
                "Link annuncio principale (opzionale)",
                value=st.session_state.get("onb_listing_url", ""),
                placeholder="https://airbnb.com/rooms/...",
                key=widget_key("onb_listing_url_input"),
            )

            detected = _onb_detect_platform(listing_url)
            if platforms and detected in platforms:
                primary_default = detected
            else:
                primary_default = platforms[0] if platforms else "airbnb"
            primary = st.selectbox(
                "Canale principale",
                options=platforms or platform_options,
                index=(platforms or platform_options).index(primary_default),
                format_func=_onb_platform_label,
                key=widget_key("onb_primary_platform_input"),
            )

            c1, c2, c3 = st.columns(3)
            with c1:
                current_price = st.number_input(
                    "Prezzo attuale",
                    min_value=10.0,
                    max_value=5000.0,
                    value=float(st.session_state.get("onb_current_price", 100.0)),
                    step=5.0,
                    key=widget_key("onb_current_price_input"),
                )
            with c2:
                min_price = st.number_input(
                    "Prezzo minimo",
                    min_value=10.0,
                    max_value=5000.0,
                    value=float(st.session_state.get("onb_min_price", 70.0)),
                    step=5.0,
                    key=widget_key("onb_min_price_input"),
                )
            with c3:
                max_price = st.number_input(
                    "Prezzo massimo",
                    min_value=10.0,
                    max_value=5000.0,
                    value=float(st.session_state.get("onb_max_price", 180.0)),
                    step=5.0,
                    key=widget_key("onb_max_price_input"),
                )

            b1, b2 = st.columns([1, 1])
            with b1:
                if st.button("Indietro", use_container_width=True, key=widget_key("onb_step2_back")):
                    st.session_state[step_key] = 1
                    st.rerun()
            with b2:
                if st.button("Avanti", type="primary", use_container_width=True, key=widget_key("onb_step2_next")):
                    if not platforms:
                        st.error("Seleziona almeno una piattaforma.")
                    elif min_price >= max_price:
                        st.error("Il prezzo minimo deve essere inferiore al prezzo massimo.")
                    elif not (min_price <= current_price <= max_price):
                        st.error("Il prezzo attuale deve stare tra minimo e massimo.")
                    else:
                        st.session_state["onb_platforms"] = list(platforms)
                        st.session_state["onb_primary_platform"] = primary
                        st.session_state["onb_listing_url"] = listing_url.strip()
                        st.session_state["onb_current_price"] = float(current_price)
                        st.session_state["onb_min_price"] = float(min_price)
                        st.session_state["onb_max_price"] = float(max_price)
                        st.session_state[step_key] = 3
                        st.rerun()

        elif step == 3:
            st.markdown(
                '<div class="onb-step-label">Passo 3 di 4</div>'
                '<div class="onb-title">Conferma il piano attivo</div>'
                '<div class="onb-desc">Il piano viene letto dall&apos;account. In futuro sara impostato dal pagamento, non dalla dashboard.</div>',
                unsafe_allow_html=True,
            )

            plan = (account.get("plan") or "free").lower()
            _render_readonly_plan_box(plan)
            st.button("Cambia piano", disabled=True, use_container_width=True, key=widget_key("onb_change_plan_disabled"))
            st.caption("Il cambio piano sara collegato al billing quando aggiungeremo Stripe/Supabase.")

            b1, b2 = st.columns([1, 1])
            with b1:
                if st.button("Indietro", use_container_width=True, key=widget_key("onb_step3_back")):
                    st.session_state[step_key] = 2
                    st.rerun()
            with b2:
                cta = {
                    "free": "Attiva consigli",
                    "plus": "Attiva approvazione",
                    "pro": "Attiva autopilot",
                }[plan]
                if st.button(cta, type="primary", use_container_width=True, key=widget_key("onb_create_property")):
                    try:
                        st.session_state["onb_plan"] = plan
                        update_account_profile(
                            account_id,
                            {
                                "name": st.session_state.get("onb_business_name", "La mia attivita"),
                            },
                        )
                        prop_data = {
                            "account_id": account_id,
                            "name": st.session_state.get("onb_property_name", "").strip(),
                            "city": st.session_state.get("onb_city_zone", "").strip(),
                            "platform": st.session_state.get("onb_primary_platform", "airbnb"),
                            "listing_url": st.session_state.get("onb_listing_url", ""),
                            "listing_id": "",
                            "min_price": float(st.session_state.get("onb_min_price", 70.0)),
                            "max_price": float(st.session_state.get("onb_max_price", 180.0)),
                            "plan": plan,
                            "sync_mode": effective_sync_mode(plan, None),
                            "strategy": "balanced",
                        }
                        existing_prop_id = st.session_state.get("onb_property_id")
                        if existing_prop_id:
                            prop = update_property(int(existing_prop_id), prop_data)
                        else:
                            prop = create_property(prop_data)
                        if not prop:
                            raise ValueError("Proprieta non trovata.")
                        prop_id = int(prop["id"])
                        st.session_state["active_prop_id"] = prop_id
                        _remember_saved_price_limits(
                            prop_id,
                            prop_data["min_price"],
                            prop_data["max_price"],
                        )
                        queue_property_pricing_widget_reset(
                            prop_id,
                            sidebar=True,
                            pricing_tab=True,
                        )
                        st.session_state["onb_property_id"] = prop_id

                        today_key = date.today().isoformat()
                        upsert_calendar_price({
                            "account_id": account_id,
                            "property_id": prop_id,
                            "date": today_key,
                            "current_price": float(st.session_state.get("onb_current_price", 100.0)),
                            "current_price_source": "onboarding",
                            "recommended_price": None,
                            "status": "current",
                            "notes": "Prezzo corrente impostato durante onboarding.",
                        })

                        platforms = st.session_state.get("onb_platforms") or []
                        primary = st.session_state.get("onb_primary_platform", "airbnb")
                        for platform in platforms:
                            upsert_property_integration({
                                "property_id": prop_id,
                                "platform": platform,
                                "listing_url": st.session_state.get("onb_listing_url", "") if platform == primary else "",
                                "listing_id": "",
                                "is_primary": 1 if platform == primary else 0,
                            })

                        st.session_state[step_key] = 4
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Errore durante la creazione: {exc}")

        elif step == 4:
            plan = st.session_state.get("onb_plan", account.get("plan") or "free")
            prop_id = st.session_state.get("onb_property_id") or st.session_state.get("active_prop_id")
            property_name = st.session_state.get("onb_property_name", "La tua proprieta")
            mode_text = {
                "free": "ricevere consigli motivati",
                "plus": "approvare le modifiche da Telegram",
                "pro": "ricevere report e notifiche operative",
            }.get(plan, "ricevere notifiche")

            st.markdown(
                '<div class="onb-step-label">Passo 4 di 4</div>'
                '<div class="onb-title">Collega Telegram</div>'
                f'<div class="onb-desc">Telegram ti serve per {mode_text}. '
                'Puoi saltare questo passaggio e collegarlo piu tardi.</div>',
                unsafe_allow_html=True,
            )

            link_data = st.session_state.get("onb_tg_link")
            if not link_data and prop_id:
                if st.button("Genera link Telegram", type="primary", use_container_width=True, key=widget_key("onb_generate_tg")):
                    try:
                        from pricepilot.services.telegram_bot import create_property_link
                        link_data = create_property_link(int(prop_id))
                        st.session_state["onb_tg_link"] = link_data
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Errore Telegram: {exc}")

            if link_data:
                deep_link = _html.escape(str(link_data.get("deep_link", "")))
                st.markdown(
                    f'<div style="background:#f0fdf4;border:1px solid #86efac;'
                    f'border-radius:12px;padding:16px 18px;margin:10px 0 16px">'
                    f'<div style="font-weight:800;color:#166534;margin-bottom:6px">'
                    f'Link pronto per { _html.escape(property_name) }</div>'
                    f'<a href="{deep_link}" target="_blank" '
                    f'style="display:inline-block;background:#229ED9;color:white;'
                    f'padding:10px 18px;border-radius:8px;font-weight:800;'
                    f'text-decoration:none">Apri Telegram</a></div>',
                    unsafe_allow_html=True,
                )

            st.markdown(
                f'<div style="background:#f8fafc;border:1px solid #e2e8f0;'
                f'border-radius:12px;padding:16px 18px;margin-top:12px">'
                f'<div class="onb-summary-row"><span class="onb-summary-label">Attivita</span>'
                f'<span class="onb-summary-value">{_html.escape(st.session_state.get("onb_business_name", account.get("name", "")))}</span></div>'
                f'<div class="onb-summary-row"><span class="onb-summary-label">Proprieta</span>'
                f'<span class="onb-summary-value">{_html.escape(property_name)}</span></div>'
                f'<div class="onb-summary-row"><span class="onb-summary-label">Piano</span>'
                f'<span class="onb-summary-value">{get_plan(plan)["label"]}</span></div>'
                f'<div class="onb-summary-row" style="border-bottom:none"><span class="onb-summary-label">Prezzi</span>'
                f'<span class="onb-summary-value">€{st.session_state.get("onb_min_price", 0):.0f} - €{st.session_state.get("onb_max_price", 0):.0f}</span></div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            b1, b2 = st.columns([1, 1])
            with b1:
                if st.button("Indietro", use_container_width=True, key=widget_key("onb_step4_back")):
                    st.session_state[step_key] = 3
                    st.rerun()
            with b2:
                if st.button("Vai alla dashboard", type="primary", use_container_width=True, key=widget_key("onb_done")):
                    active_prop_id = st.session_state.get("active_prop_id")
                    _clear_onboarding_state()
                    if active_prop_id:
                        st.session_state["active_prop_id"] = active_prop_id
                    st.rerun()


def _parse_dec_reasons(notes: str, factors: str, pct: float) -> list:
    """
    Ritorna lista di (icon, testo_italiano) per le reason bullets di una decisione.
    Priorità: factors JSON > notes stringa > fallback generico da pct.
    """
    import json as _json

    # Mapping: chiave fattore → (icona, testo_positivo, testo_negativo)
    _FACTOR = {
        "seasonality": ("📅", "Alta stagione — domanda elevata",           "Bassa stagione — domanda ridotta"),
        "weekend":     ("📆", "Aumento domanda nel weekend",               "Giorno infrasettimanale — domanda bassa"),
        "event":       ("🎉", "Evento locale che aumenta la domanda",      "Nessun evento rilevante nel periodo"),
        "occupancy":   ("📊", "Alta occupazione — prezzo alzato",          "Bassa occupazione — prezzo ridotto"),
        "competitor":  ("🏆", "I competitor hanno alzato i prezzi",        "I competitor hanno abbassato i prezzi"),
        "demand":      ("📈", "Domanda di mercato in aumento",             "Domanda di mercato in calo"),
        "strategy":    ("🎯", "Strategia premium applicata",               "Strategia conservativa — prezzi stabili"),
        "min_price":   ("🔒", "Prezzo minimo applicato",                   "Prezzo minimo applicato"),
        "max_price":   ("🔒", "Prezzo massimo raggiunto",                  "Prezzo massimo raggiunto"),
        "location":    ("📍", "Zona ad alta domanda",                      "Zona a bassa domanda — adeguato"),
    }

    bullets = []

    # ── Priority 1: factors JSON breakdown ────────────────────────────────
    if factors:
        try:
            bd = _json.loads(factors) if isinstance(factors, str) else {}
            relevant = [
                (k, float(v)) for k, v in bd.items()
                if k != "base" and abs(float(v)) > 0.5
            ]
            relevant.sort(key=lambda x: abs(x[1]), reverse=True)
            for key, val in relevant[:3]:
                if key in _FACTOR:
                    icon, pos_txt, neg_txt = _FACTOR[key]
                    bullets.append((icon, pos_txt if val > 0 else neg_txt))
        except Exception:
            pass

    # ── Priority 2: parse notes string ────────────────────────────────────
    if not bullets and notes:
        _NOTE_KW = [
            ("weekend",    "📆", "Alta domanda nel weekend"),
            ("event",      "🎉", "Evento locale in corso"),
            ("competitor", "🏆", "Variazione prezzi competitor"),
            ("occupanc",   "📊", "Variazione dell'occupazione"),
            ("stagional",  "📅", "Stagionalità del periodo"),
            ("seasonal",   "📅", "Stagionalità del periodo"),
            ("demand",     "📈", "Variazione della domanda di mercato"),
            ("premium",    "🎯", "Strategia premium attiva"),
            ("conservat",  "🎯", "Strategia conservativa attiva"),
            ("mercato",    "🏘️", "Condizioni di mercato cambiate"),
            ("minimo",     "🔒", "Prezzo minimo applicato"),
            ("massimo",    "🔒", "Prezzo massimo raggiunto"),
        ]
        raw_parts = [
            p.strip() for p in notes.split("|")
            if p.strip()
            and not p.strip().startswith("conf=")
            and not p.strip().startswith("event=")
            and not p.strip().startswith("plan=")
            and not p.strip().startswith("strategy=")
        ]
        seen_icons = set()
        for part in raw_parts[:5]:
            part_lower = part.lower()
            matched = False
            for kw, icon, txt in _NOTE_KW:
                if kw in part_lower and icon not in seen_icons:
                    bullets.append((icon, txt))
                    seen_icons.add(icon)
                    matched = True
                    break
            if not matched and len(part) > 3 and len(bullets) < 3:
                # Show raw note as last resort, truncated and cleaned
                clean = part[:55].replace("_", " ").capitalize()
                bullets.append(("💡", clean))

    # ── Priorità 3: generico in base alla direzione ───────────────────────
    if not bullets:
        if pct > 2:
            bullets = [
                ("📈", "La domanda di mercato è in crescita"),
                ("🏆", "I competitor hanno prezzi più alti"),
            ]
        elif pct < -2:
            bullets = [
                ("📉", "Domanda in calo — prezzo ottimizzato per l'occupazione"),
                ("💡", "Prezzo adeguato per restare competitivi"),
            ]
        else:
            bullets = [("✅", "Il prezzo è già ottimale per le condizioni di mercato")]

    return bullets[:3]


def tab_home(cfg: dict):
    """Home – Pannello di controllo Autopilot."""
    from pricepilot.core.database import (
        get_decisions, get_pending_approvals, get_conn,
        update_calendar_status_for_decision,
    )
    from pricepilot.engine.decision_engine import approve_decision
    from pricepilot.engine.market_analyzer import simulate_competitors, calculate_market_stats
    from pricepilot.data_sources.events import get_event_for_date, event_to_string
    from pricepilot.core.scheduler import run_pricing_cycle
    from pricepilot.providers.registry import get_billing_provider
    import hashlib
    import json as _json

    account_id = current_account_id()
    props = list_properties(account_id=account_id)

    today = date.today()
    now   = datetime.now()
    last_run = get_last_operation_run(account_id)
    account = get_account(account_id) or {"plan": "free"}
    current_plan = str(account.get("plan", "free")).lower()
    current_user = get_current_user() or {}
    can_run_manual_cycle = get_billing_provider().can_run_manual_cycle(
        account=account,
        user=current_user,
    )

    # ── ONBOARDING se nessuna proprietà ──────────────────────────────────────
    if not props:
        _tab_onboarding_v2("home")
        return

    # ── Proprietà attiva di riferimento ──────────────────────────────────────
    active_prop_id = st.session_state.get("active_prop_id")
    active_prop = next((p for p in props if p["id"] == active_prop_id), props[0])

    # ══════════════════════════════════════════════════════════════════════════
    # CALCOLO DATI AGGREGATI (tutte le proprietà)
    # ══════════════════════════════════════════════════════════════════════════
    _all_prices     = []
    _all_occ_30d    = []
    _total_comp_sim = 0

    _evt_today     = get_event_for_date(today)
    _evt_str_today = event_to_string(_evt_today)

    for _p in props:
        _base_p = (_p["min_price"] + _p["max_price"]) / 2
        try:
            _comps_p = simulate_competitors(_base_p, today, _evt_str_today, 8)
            _ms_p    = calculate_market_stats(_comps_p)
            _total_comp_sim += len(_comps_p)
            _h_o = int(hashlib.md5((str(_p["id"]) + today.isoformat()).encode()).hexdigest(), 16)
            _occ_p = round(0.45 + (_h_o % 1000) / 1000 * 0.45, 2)
            _rec_p = calculate_recommended_price(
                base_price=_base_p,
                market_avg=_ms_p["market_avg"],
                occupancy=_occ_p,
                target_date=today,
                has_event=(_evt_today is not None),
                min_price=float(_p["min_price"]),
                max_price=float(_p["max_price"]),
                competitor_count=len(_comps_p),
            )
            _all_prices.append(_rec_p["recommended_price"])
        except Exception:
            _all_prices.append(_base_p)

        # Occupazione media 30 giorni per questa proprietà
        _occ_vals_p = []
        for _i in range(30):
            _d = today + timedelta(days=_i)
            _h = int(hashlib.md5((str(_p["id"]) + _d.isoformat()).encode()).hexdigest(), 16)
            _occ_vals_p.append(round(0.45 + (_h % 1000) / 1000 * 0.45, 2))
        _all_occ_30d.append(sum(_occ_vals_p) / len(_occ_vals_p))

    avg_all_prices = sum(_all_prices) / len(_all_prices) if _all_prices else 0
    avg_all_occ    = sum(_all_occ_30d) / len(_all_occ_30d) if _all_occ_30d else 0

    # Proprietà attiva: prezzo oggi e media mercato (per posizione nel mercato)
    _ap_idx      = next((i for i, p in enumerate(props) if p["id"] == active_prop["id"]), 0)
    price_oggi   = _all_prices[_ap_idx] if _ap_idx < len(_all_prices) else avg_all_prices
    occ_30d_avg  = _all_occ_30d[_ap_idx] if _ap_idx < len(_all_occ_30d) else avg_all_occ
    comps_kpi       = []
    _base_mid       = (active_prop["min_price"] + active_prop["max_price"]) / 2
    market_avg_oggi = _base_mid
    market_min_oggi = _base_mid * 0.75
    market_max_oggi = _base_mid * 1.35
    market_comp_cnt = 0
    try:
        comps_kpi       = simulate_competitors(_base_mid, today, _evt_str_today, 10)
        _mstats_kpi     = calculate_market_stats(comps_kpi)
        market_avg_oggi = _mstats_kpi["market_avg"]
        market_min_oggi = _mstats_kpi.get("market_min", market_avg_oggi * 0.80)
        market_max_oggi = _mstats_kpi.get("market_max", market_avg_oggi * 1.30)
        market_comp_cnt = _mstats_kpi.get("competitor_count", len(comps_kpi))
    except Exception:
        pass

    delta_vs_mkt  = ((price_oggi - market_avg_oggi) / max(market_avg_oggi, 1)) * 100
    pos_vs_market = delta_vs_mkt   # alias used in alert block

    if delta_vs_mkt > 5:
        mkt_position = "📈 Sopra mercato"
        mkt_pos_bg, mkt_pos_fg = "#fef2f2", "#991b1b"
    elif delta_vs_mkt < -5:
        mkt_position = "📉 Sotto mercato"
        mkt_pos_bg, mkt_pos_fg = "#f0fdf4", "#166534"
    else:
        mkt_position = "✅ In linea con il mercato"
        mkt_pos_bg, mkt_pos_fg = "#eff6ff", "#1e40af"

    # Ultimo aggiornamento e prossima analisi
    _last_update_str = ""
    if last_run:
        _run_ts = str(last_run.get("finished_at") or last_run.get("started_at") or "")
        if len(_run_ts) >= 16:
            _last_update_str = _run_ts[11:16]
    try:
        _last_dec_ts = get_decision_log(property_id=None, limit=1, account_id=account_id)
        if not _last_update_str and _last_dec_ts:
            _ts_raw = str(_last_dec_ts[0].get("timestamp", ""))
            if len(_ts_raw) >= 16:
                _last_update_str = _ts_raw[11:16]
    except Exception:
        pass
    if not _last_update_str:
        # Fallback: ora attuale arrotondata al quarto d'ora precedente
        _last_h = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        _last_update_str = _last_h.strftime("%H:%M")

    _next_h = now + timedelta(hours=6)
    if last_run and last_run.get("next_run_at"):
        try:
            _next_h = datetime.fromisoformat(str(last_run["next_run_at"])[:19])
        except Exception:
            pass
    _next_update_str = _next_h.strftime("%H:%M")

    # ── Relative time labels for autopilot card ───────────────────────────────
    _time_since_lbl = ""
    try:
        if last_run and (last_run.get("finished_at") or last_run.get("started_at")):
            _ts_raw2 = str(last_run.get("finished_at") or last_run.get("started_at"))
            if len(_ts_raw2) >= 16:
                _dec_dt = datetime.fromisoformat(_ts_raw2[:19])
                _mins_ago = int((now - _dec_dt).total_seconds() / 60)
                if _mins_ago < 2:
                    _time_since_lbl = "adesso"
                elif _mins_ago < 60:
                    _time_since_lbl = f"{_mins_ago} min fa"
                elif _mins_ago < 1440:
                    _time_since_lbl = f"{_mins_ago // 60}h fa"
                else:
                    _time_since_lbl = f"{_mins_ago // 1440}g fa"
        elif _last_dec_ts:
            _ts_raw2 = str(_last_dec_ts[0].get("timestamp", ""))
            if len(_ts_raw2) >= 16:
                _dec_dt  = datetime.strptime(_ts_raw2[:16], "%Y-%m-%d %H:%M")
                _mins_ago = int((now - _dec_dt).total_seconds() / 60)
                if _mins_ago < 2:
                    _time_since_lbl = "adesso"
                elif _mins_ago < 60:
                    _time_since_lbl = f"{_mins_ago} min fa"
                elif _mins_ago < 1440:
                    _time_since_lbl = f"{_mins_ago // 60}h fa"
                else:
                    _time_since_lbl = f"{_mins_ago // 1440}g fa"
    except Exception:
        pass
    _mins_to_next   = max(0, int((_next_h - now).total_seconds() / 60))
    _next_run_lbl   = f"tra {_mins_to_next} min" if _mins_to_next > 1 else "a momenti"

    # ══════════════════════════════════════════════════════════════════════════
    # PART 1 — PLAN-AWARE STATUS CARD
    # ══════════════════════════════════════════════════════════════════════════
    _comp_display   = str(_total_comp_sim) if _total_comp_sim > 0 else str(len(comps_kpi))
    _plan_home = {
        "free": {
            "title": "Consigli prezzo",
            "badge": "FREE",
            "subtitle": "PricePilot analizza e invia consigli. Aggiornamento manuale sulle OTA.",
        },
        "plus": {
            "title": "Approvazione prezzi",
            "badge": "PLUS",
            "subtitle": "PricePilot analizza e prepara modifiche. Tu approvi da Telegram.",
        },
        "pro": {
            "title": "Autopilot",
            "badge": "PRO",
            "subtitle": "PricePilot analizza e lavora in autonomia con regole di sicurezza.",
        },
    }.get(current_plan, {
        "title": "Monitoraggio prezzi",
        "badge": current_plan.upper() if current_plan else "PIANO",
        "subtitle": "PricePilot monitora mercato, eventi e occupazione.",
    })
    _ap_subtitle = (
        f"{_plan_home['subtitle']} Aggiornato {_time_since_lbl}."
        if _time_since_lbl else
        f"{_plan_home['subtitle']} Attivo ora."
    )
    st.markdown(
        '<div class="autopilot-card">'

        # ── Header row ──────────────────────────────────────────────────────
        '<div class="autopilot-header">'
        '<span class="autopilot-dot"></span>'
        '<div style="flex:1">'
        '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">'
        f'<div class="autopilot-title">{_plan_home["title"]}</div>'
        '<span style="background:rgba(74,222,128,0.15);color:#4ade80;font-size:0.62rem;'
        'font-weight:800;letter-spacing:0.10em;padding:3px 10px;border-radius:20px;'
        f'border:1px solid rgba(74,222,128,0.30)">{_plan_home["badge"]}</span>'
        '</div>'
        f'<div class="autopilot-subtitle">{_ap_subtitle}</div>'
        '</div>'
        '</div>'

        # ── Stats grid ──────────────────────────────────────────────────────
        '<div class="autopilot-grid">'

        f'<div class="autopilot-stat">'
        f'<div class="autopilot-stat-label">Ultima analisi mercato</div>'
        f'<div class="autopilot-stat-value">{_last_update_str}</div>'
        f'<div class="autopilot-stat-sub">'
        f'{_time_since_lbl if _time_since_lbl else "oggi"}</div>'
        f'</div>'

        f'<div class="autopilot-stat">'
        f'<div class="autopilot-stat-label">Prossima analisi</div>'
        f'<div class="autopilot-stat-value">{_next_update_str}</div>'
        f'<div class="autopilot-stat-sub">{_next_run_lbl}</div>'
        f'</div>'

        f'<div class="autopilot-stat">'
        f'<div class="autopilot-stat-label">Proprietà monitorate</div>'
        f'<div class="autopilot-stat-value">{len(props)}</div>'
        f'<div class="autopilot-stat-sub">attivo ora</div>'
        f'</div>'

        f'<div class="autopilot-stat">'
        f'<div class="autopilot-stat-label">Competitor monitorati</div>'
        f'<div class="autopilot-stat-value">{_comp_display}</div>'
        f'<div class="autopilot-stat-sub">per ciclo di analisi</div>'
        f'</div>'

        '</div>'
        '</div>',
        unsafe_allow_html=True,
    )

    c_run, c_run_info = st.columns([1, 3])
    _run_status = str((last_run or {}).get("status") or "").lower()
    _run_is_running = _run_status == "running"
    with c_run:
        if st.button(
            "Esegui ciclo ora",
            use_container_width=True,
            key="manual_pricing_cycle",
            disabled=(not can_run_manual_cycle or _run_is_running),
        ):
            with st.spinner("Analisi mercato e decisioni in corso..."):
                outcome = run_pricing_cycle(account_id=account_id, source="dashboard_manual")
            run = outcome.get("run") or {}
            if outcome.get("skipped"):
                st.warning("Ciclo non avviato: ne esiste gia uno in esecuzione.")
            else:
                st.success(f"Ciclo completato: {run.get('decisions_count', 0)} decisioni generate.")
            st.rerun()
    with c_run_info:
        status_labels = {
            "running": "in esecuzione",
            "success": "completato",
            "partial_error": "completato con errori",
            "error": "errore",
            "stale": "scaduto",
        }
        if last_run:
            st.caption(
                f"Ultimo ciclo: {status_labels.get(_run_status, _run_status or 'n/d')} · "
                f"{last_run.get('decisions_count', 0)} decisioni · "
                f"prossimo controllo stimato alle {_next_update_str}"
            )
        else:
            st.caption("Il ciclo automatico e pronto: eseguilo una volta per iniziare a popolare run, audit e decisioni.")
        if _run_is_running:
            st.warning("Ciclo gia in esecuzione: PricePilot blocca avvii doppi finche non termina.", icon="!")
        elif not can_run_manual_cycle:
            st.caption("Il ciclo manuale e disponibile solo in ambiente dev/admin. In produzione parte dallo scheduler.")

    # ══════════════════════════════════════════════════════════════════════════
    # PART 2 — SUMMARY METRIC CARDS
    # ══════════════════════════════════════════════════════════════════════════
    with st.expander("Storico cicli pricing", expanded=False):
        runs = get_operation_runs(limit=12, account_id=account_id)
        if not runs:
            st.info("Nessun ciclo registrato.")
        else:
            rows = []
            latest_errors = []
            for run_row in runs:
                try:
                    summary = _json.loads(run_row.get("summary") or "{}")
                except Exception:
                    summary = {}
                errors = summary.get("errors") or []
                if errors and not latest_errors:
                    latest_errors = errors
                started = str(run_row.get("started_at") or "")[:16].replace("T", " ")
                finished = str(run_row.get("finished_at") or "")[:16].replace("T", " ")
                next_run = str(run_row.get("next_run_at") or "")[:16].replace("T", " ")
                rows.append({
                    "Inizio": started,
                    "Fine": finished or "in corso",
                    "Stato": status_labels.get(str(run_row.get("status") or "").lower(), run_row.get("status", "")),
                    "Decisioni": int(run_row.get("decisions_count") or 0),
                    "Errori": len(errors),
                    "Origine": run_row.get("source", ""),
                    "Prossimo": next_run,
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            if latest_errors:
                st.markdown("**Ultimi errori per proprieta**")
                for err in latest_errors[:5]:
                    st.caption(
                        f"{err.get('property_name') or 'Proprieta'} "
                        f"(ID {err.get('property_id', '-')}) · {err.get('error', '')}"
                    )

    st.markdown('<div style="height:14px"></div>', unsafe_allow_html=True)
    sm1, sm2, sm3 = st.columns(3)

    with sm1:
        _n_props_label = f'su {len(props)} {"proprietà" if len(props) == 1 else "proprietà"}'
        st.markdown(
            f'<div class="summary-metric-card">'
            f'<div class="sm-icon">💰</div>'
            f'<div class="sm-label">Prezzo consigliato · stasera</div>'
            f'<div class="sm-value">€{avg_all_prices:.0f}</div>'
            f'<div class="sm-sub">{_n_props_label}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    with sm2:
        _occ_pct_avg   = int(avg_all_occ * 100)
        _occ_col_avg   = "#16a34a" if avg_all_occ >= 0.65 else ("#ca8a04" if avg_all_occ >= 0.40 else "#dc2626")
        _occ_trend_lbl = "Alta" if avg_all_occ >= 0.65 else ("Media" if avg_all_occ >= 0.40 else "Bassa")
        st.markdown(
            f'<div class="summary-metric-card">'
            f'<div class="sm-icon">📅</div>'
            f'<div class="sm-label">Occupazione · prossimi 30 giorni</div>'
            f'<div class="sm-value" style="color:{_occ_col_avg}">{_occ_pct_avg}%</div>'
            f'<div class="sm-sub">{_occ_trend_lbl} · ≈ {round(avg_all_occ * 30)} notti prenotate</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    with sm3:
        st.markdown(
            f'<div class="summary-metric-card">'
            f'<div class="sm-icon">🏆</div>'
            f'<div class="sm-label">Posizione nel mercato · oggi</div>'
            f'<div class="sm-badge" style="background:{mkt_pos_bg};color:{mkt_pos_fg}">'
            f'{mkt_position}</div>'
            f'<div class="sm-sub">{delta_vs_mkt:+.1f}% vs media competitor</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # ══════════════════════════════════════════════════════════════════════════
    # PART 2.5 — CONTESTO DI MERCATO (Market Intelligence)
    # ══════════════════════════════════════════════════════════════════════════
    st.markdown('<div class="section-title">🏘️ Contesto di Mercato</div>', unsafe_allow_html=True)

    # ── 4 metric cards ────────────────────────────────────────────────────────
    _mi1, _mi2, _mi3, _mi4 = st.columns(4)

    # 1 – Media mercato
    with _mi1:
        st.markdown(
            f'<div class="mkt-metric">'
            f'<div class="mkt-metric-icon">📊</div>'
            f'<div class="mkt-metric-label">Media mercato</div>'
            f'<div class="mkt-metric-value">€{market_avg_oggi:.0f}</div>'
            f'<div class="mkt-metric-sub">{market_comp_cnt} competitor</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    # 2 – Minimo mercato
    with _mi2:
        st.markdown(
            f'<div class="mkt-metric">'
            f'<div class="mkt-metric-icon">📉</div>'
            f'<div class="mkt-metric-label">Minimo mercato</div>'
            f'<div class="mkt-metric-value">€{market_min_oggi:.0f}</div>'
            f'<div class="mkt-metric-sub">prezzo più basso oggi</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    # 3 – Massimo mercato
    with _mi3:
        st.markdown(
            f'<div class="mkt-metric">'
            f'<div class="mkt-metric-icon">📈</div>'
            f'<div class="mkt-metric-label">Massimo mercato</div>'
            f'<div class="mkt-metric-value">€{market_max_oggi:.0f}</div>'
            f'<div class="mkt-metric-sub">prezzo più alto oggi</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    # 4 – Il tuo prezzo medio (highlighted)
    _yours_val_col = mkt_pos_fg
    with _mi4:
        st.markdown(
            f'<div class="mkt-metric mkt-metric-yours" '
            f'style="border-color:{mkt_pos_bg};">'
            f'<div class="mkt-metric-icon">🏠</div>'
            f'<div class="mkt-metric-label">Il tuo prezzo medio</div>'
            f'<div class="mkt-metric-value" style="color:{_yours_val_col}">€{avg_all_prices:.0f}</div>'
            f'<div class="mkt-metric-sub">{delta_vs_mkt:+.1f}% vs media mercato</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    # ── Position bar + explanation ─────────────────────────────────────────────
    # Visual % position of your price in the market range (0 = min, 100 = max)
    _mkt_range   = max(market_max_oggi - market_min_oggi, 1)
    _your_pos_pct = max(0, min(100, (avg_all_prices - market_min_oggi) / _mkt_range * 100))
    _avg_pos_pct  = 50.0  # market average is always the midpoint

    # Position label mapping
    if delta_vs_mkt > 5:
        _pos_label = "📈 Sopra mercato"
        _pos_badge_bg, _pos_badge_fg = "#fee2e2", "#991b1b"
        _pos_explain = (
            "Il tuo prezzo è più alto della media dei competitor. "
            "PricePilot monitora l'occupazione per trovare il punto ottimale."
        )
    elif delta_vs_mkt < -5:
        _pos_label = "📉 Sotto mercato"
        _pos_badge_bg, _pos_badge_fg = "#dcfce7", "#166534"
        _pos_explain = (
            "Il tuo prezzo è più basso della media dei competitor. "
            "Potresti aumentarlo senza perdere prenotazioni."
        )
    else:
        _pos_label = "✅ In linea col mercato"
        _pos_badge_bg, _pos_badge_fg = "#dbeafe", "#1e40af"
        _pos_explain = (
            "Il tuo prezzo è allineato con la media di mercato. "
            "PricePilot continuerà ad adeguarlo in base alla domanda."
        )

    # Dot colour for the visual strip
    _dot_col = _pos_badge_fg

    st.markdown(
        f'<div class="mkt-context-bar">'

        # Left: position label
        f'<div style="flex-shrink:0">'
        f'<div class="mkt-position-lbl">Posizione del tuo listing nel mercato</div>'
        f'<div style="margin-top:6px">'
        f'<span class="mkt-pos-badge" '
        f'style="background:{_pos_badge_bg};color:{_pos_badge_fg}">'
        f'{_pos_label}</span>'
        f'</div>'

        # Mini visual strip (min ←——●——→ max)
        f'<div class="mkt-bar-track" style="width:160px;margin-top:8px">'
        f'<div class="mkt-bar-range" style="left:0;width:100%"></div>'
        # Market average dot (grey)
        f'<div class="mkt-bar-dot" '
        f'style="left:{_avg_pos_pct:.0f}%;background:#94a3b8" '
        f'title="Media mercato €{market_avg_oggi:.0f}"></div>'
        # Your price dot (coloured)
        f'<div class="mkt-bar-dot" '
        f'style="left:{_your_pos_pct:.0f}%;background:{_dot_col};z-index:2" '
        f'title="Tuo prezzo €{avg_all_prices:.0f}"></div>'
        f'</div>'
        f'<div style="display:flex;justify-content:space-between;width:160px;'
        f'font-size:0.62rem;color:#94a3b8;margin-top:2px">'
        f'<span>€{market_min_oggi:.0f}</span>'
        f'<span>€{market_max_oggi:.0f}</span>'
        f'</div>'
        f'</div>'

        # Right: explanation text
        f'<div class="mkt-explanation">'
        f'{_pos_explain}<br>'
        f'<span style="font-size:0.74rem;color:#94a3b8;display:block;margin-top:4px">'
        f'PricePilot analizza i prezzi dei competitor per capire se il tuo '
        f'appartamento è sopra o sotto il mercato.</span>'
        f'</div>'

        f'</div>',
        unsafe_allow_html=True,
    )

    st.markdown("---")

    # ══════════════════════════════════════════════════════════════════════════
    # PART 3 — LE TUE PROPRIETÀ
    # ══════════════════════════════════════════════════════════════════════════
    st.markdown('<div class="section-title">🏠 Le Tue Proprietà</div>', unsafe_allow_html=True)

    # Modalità: colori e label in italiano
    _mode_colors = {
        "advisory": "#9ca3af",   # grigio neutro
        "approval": "#f59e0b",
        "auto":     "#10b981",
    }
    _mode_labels = {
        "advisory": "Modalità manuale",
        "approval": "✅ Approvazione",
        "auto":     "🤖 Automatico",
    }
    ncols = min(len(props), 3)
    cols  = st.columns(ncols)

    for idx, prop in enumerate(props):
        mode   = prop.get("sync_mode", "advisory")
        mcolor = _mode_colors.get(mode, "#9ca3af")
        mlabel = _mode_labels.get(mode, mode)

        # Occupazione hash-based (coerente per proprietà)
        h = int(hashlib.md5(str(prop["id"]).encode()).hexdigest(), 16)
        occ_prop     = round(0.45 + (h % 1000) / 1000 * 0.45, 2)
        occ_pct      = int(occ_prop * 100)
        booked_nights = round(occ_prop * 30)

        # Prezzo suggerito oggi (ultimo dal log, fallback midpoint)
        try:
            _last_dec  = get_decision_log(property_id=prop["id"], limit=1, account_id=account_id)
            today_price = float(_last_dec[0]["new_price"]) if _last_dec else None
        except Exception:
            today_price = None
        if today_price is None:
            today_price = float(prop["min_price"] + prop["max_price"]) / 2

        # Prezzo medio dalle decisioni recenti
        try:
            _hist = get_decision_log(property_id=prop["id"], limit=30, account_id=account_id)
            _prices_h = [float(d["new_price"]) for d in _hist if d.get("new_price")]
            avg_price = round(sum(_prices_h) / len(_prices_h), 0) if _prices_h else (
                float(prop["min_price"] + prop["max_price"]) / 2
            )
        except Exception:
            avg_price = float(prop["min_price"] + prop["max_price"]) / 2

        try:
            from pricepilot.core.database import get_telegram_link_by_property
            tg = get_telegram_link_by_property(prop["id"])
            tg_status = "🔔" if (tg and tg.get("chat_id")) else "🔕"
        except Exception:
            tg_status = "—"

        _pname = _html.escape(str(prop.get('name', '')))
        _pcity = _html.escape(str(prop.get('city', '') or '—'))
        _pplat_raw = str(prop.get('platform', '') or '').lower()
        _plat_labels = {
            "airbnb": "Airbnb", "booking": "Booking.com",
            "vrbo": "Vrbo",     "direct": "Diretto", "other": "Altro",
        }
        _pplat = _html.escape(_plat_labels.get(_pplat_raw, _pplat_raw.upper() or '—'))

        # Colore bordo card basato sul modo
        _border_col = mcolor if mode != "advisory" else "#e5e7eb"

        with cols[idx % ncols]:
            st.markdown(
                f'<div style="background:white;border:2px solid {_border_col};'
                f'border-radius:14px;padding:18px 18px 14px;margin-bottom:12px;'
                f'box-shadow:0 2px 8px rgba(0,0,0,.06)">'

                f'<div style="font-size:1.08rem;font-weight:800;color:#111827;margin-bottom:3px">'
                f'{_pname}</div>'

                f'<div style="font-size:0.82rem;color:#6b7280;margin-bottom:1px">📍 {_pcity}</div>'

                f'<div style="font-size:0.78rem;color:#9ca3af;margin-bottom:8px">{_pplat}'
                f'&nbsp;·&nbsp;'
                f'<span style="background:#f3f4f6;color:#6b7280;padding:2px 8px;'
                f'border-radius:20px;font-size:0.73rem;font-weight:500;cursor:default">'
                f'{mlabel}</span>'
                f'&nbsp;{tg_status}</div>'

                f'<div style="background:#f8fafc;border-radius:10px;padding:12px 14px;margin-bottom:8px">'
                f'<div style="font-size:0.68rem;color:#94a3b8;text-transform:uppercase;'
                f'letter-spacing:0.05em;margin-bottom:4px">Prezzo suggerito oggi</div>'
                f'<div style="font-size:1.6rem;font-weight:900;color:#1e293b">€{today_price:.0f}</div>'
                f'</div>'

                f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">'

                f'<div style="background:#f8fafc;border-radius:8px;padding:10px 8px;text-align:center">'
                f'<div style="font-size:0.68rem;color:#94a3b8;margin-bottom:3px;'
                f'cursor:default" title="Percentuale di notti prenotate negli ultimi 30 giorni.">'
                f'Occupazione ultimi 30 giorni ℹ</div>'
                f'<div style="font-size:1.25rem;font-weight:800;color:#334155">{occ_pct}%</div>'
                f'<div style="font-size:0.68rem;color:#94a3b8;margin-top:2px">'
                f'≈ {booked_nights} notti su 30</div>'
                f'</div>'

                f'<div style="background:#f8fafc;border-radius:8px;padding:10px 8px;text-align:center">'
                f'<div style="font-size:0.68rem;color:#94a3b8;margin-bottom:3px">Prezzo medio</div>'
                f'<div style="font-size:1.25rem;font-weight:800;color:#334155">€{avg_price:.0f}</div>'
                f'<div style="font-size:0.68rem;color:#94a3b8;margin-top:2px">ultimi 30 gg</div>'
                f'</div>'

                f'</div>'

                f'<div style="font-size:0.75rem;color:#9ca3af;padding-top:6px;'
                f'border-top:1px solid #f1f5f9">'
                f'Min €{prop["min_price"]:.0f} &nbsp;·&nbsp; Max €{prop["max_price"]:.0f}'
                f'</div>'

                f'</div>',
                unsafe_allow_html=True,
            )

    st.markdown("---")

    # ══════════════════════════════════════════════════════════════════════════════
    # PART 4 — DECISION FEED
    # ══════════════════════════════════════════════════════════════════════════════
    st.markdown('<div class="section-title">🤖 Ultime Decisioni di Pricing</div>', unsafe_allow_html=True)
    st.caption("Ogni aggiornamento di prezzo effettuato da PricePilot, con la spiegazione del motivo.")

    _MESI_IT = ["gen","feb","mar","apr","mag","giu","lug","ago","set","ott","nov","dic"]

    try:
        recent_log = get_decision_log(property_id=None, limit=6, account_id=account_id)
        prop_map_d = {p["id"]: p["name"] for p in props}

        if not recent_log:
            # Placeholder card
            st.markdown(
                '<div class="dec-empty">'
                '<div style="font-size:2.4rem;margin-bottom:12px">🤖</div>'
                '<div style="font-size:1rem;font-weight:700;color:#374151;margin-bottom:8px">'
                'Nessuna decisione ancora</div>'
                '<div style="font-size:0.88rem;color:#6b7280;max-width:440px;'
                'margin:0 auto;line-height:1.65">'
                'Il motore è attivo. Le prime decisioni di pricing appariranno qui '
                'dopo la prossima analisi di mercato.</div>'
                '</div>',
                unsafe_allow_html=True,
            )
        else:
            # 2-column grid of decision cards
            _dec_cols = st.columns(2)
            for _di, d in enumerate(recent_log):
                old_p   = float(d.get("old_price") or 0)
                new_p   = float(d.get("new_price") or 0)
                pct     = ((new_p - old_p) / max(old_p, 1)) * 100 if old_p else 0
                notes   = str(d.get("notes", "") or d.get("decision", "") or "")
                factors = str(d.get("factors") or "")
                applied = d.get("applied")
                ts_raw  = str(d.get("timestamp", ""))
                pname   = _html.escape(prop_map_d.get(d.get("property_id"), "—"))

                # ── Direction indicators ──────────────────────────────────────
                if pct > 2:
                    _card_mod   = "dec-card-up"
                    _action_mod = "dec-action-up"
                    _action_txt = "↑ Prezzo aumentato"
                    _price_mod  = "dec-price-new-up"
                    _pct_bg, _pct_fg = "#dcfce7", "#166534"
                elif pct < -2:
                    _card_mod   = "dec-card-down"
                    _action_mod = "dec-action-down"
                    _action_txt = "↓ Prezzo ridotto"
                    _price_mod  = "dec-price-new-down"
                    _pct_bg, _pct_fg = "#fee2e2", "#991b1b"
                else:
                    _card_mod   = "dec-card-flat"
                    _action_mod = "dec-action-flat"
                    _action_txt = "→ Prezzo confermato"
                    _price_mod  = "dec-price-new-flat"
                    _pct_bg, _pct_fg = "#f3f4f6", "#6b7280"

                # ── Status badge ──────────────────────────────────────────────
                if applied == 1:
                    _status_html = '<span class="badge-green">✅ Applicato</span>'
                elif "REJECTED" in notes.upper():
                    _status_html = '<span class="badge-red">❌ Rifiutato</span>'
                elif applied is None or applied == -1:
                    _status_html = '<span class="badge-yellow">⏳ In attesa</span>'
                else:
                    _status_html = '<span class="badge-blue">💡 Suggerito</span>'

                # ── Timestamp → Italian format ────────────────────────────────
                _ts_display = "—"
                if len(ts_raw) >= 16:
                    try:
                        _dtp = datetime.strptime(ts_raw[:16], "%Y-%m-%d %H:%M")
                        _ts_display = (
                            f"{_dtp.strftime('%H:%M')}"
                            f" · {_dtp.day} {_MESI_IT[_dtp.month - 1]} {_dtp.year}"
                        )
                    except Exception:
                        _ts_display = ts_raw[:16]

                # ── Price block ────────────────────────────────────────────────
                if old_p > 0:
                    _price_block = (
                        f'<div class="dec-prices">'
                        f'<span class="dec-price-old">€{old_p:.0f}</span>'
                        f'<span class="dec-price-sep">→</span>'
                        f'<span class="dec-price-new {_price_mod}">€{new_p:.0f}</span>'
                        f'<span class="dec-pct-badge" '
                        f'style="background:{_pct_bg};color:{_pct_fg}">'
                        f'{pct:+.1f}%</span>'
                        f'</div>'
                    )
                else:
                    _price_block = (
                        f'<div class="dec-prices">'
                        f'<span class="dec-price-new {_price_mod}">€{new_p:.0f}</span>'
                        f'</div>'
                    )

                # ── Signal pills (what changed — quick scan row) ──────────────
                import json as _json_dec
                _sig_pills_html = ""
                _SIG_PILL_MAP = {
                    "weekend":     ("📆", "Weekend"),
                    "event":       ("🎉", "Evento"),
                    "occupancy":   ("📊", "Occupazione"),
                    "competitor":  ("🏆", "Competitor"),
                    "demand":      ("📈", "Domanda"),
                    "seasonality": ("📅", "Stagionalità"),
                    "strategy":    ("🎯", "Strategia"),
                }
                if factors:
                    try:
                        _bd_pills = _json_dec.loads(factors) if isinstance(factors, str) else {}
                        _top_sigs = sorted(
                            [(k, abs(float(v))) for k, v in _bd_pills.items()
                             if k != "base" and abs(float(v)) > 0.5],
                            key=lambda x: x[1], reverse=True,
                        )[:3]
                        _pill_items = [
                            f'<span style="background:#f1f5f9;color:#475569;font-size:0.67rem;'
                            f'font-weight:600;padding:2px 9px;border-radius:20px;'
                            f'white-space:nowrap;border:1px solid #e2e8f0">'
                            f'{_SIG_PILL_MAP[_sk][0]} {_SIG_PILL_MAP[_sk][1]}</span>'
                            for _sk, _ in _top_sigs if _sk in _SIG_PILL_MAP
                        ]
                        if _pill_items:
                            _sig_pills_html = (
                                f'<div style="display:flex;gap:5px;flex-wrap:wrap;margin-bottom:10px">'
                                + "".join(_pill_items)
                                + "</div>"
                            )
                    except Exception:
                        pass

                # ── Confidence score (parse from notes: "conf=0.87") ──────────
                import re as _re_dec
                _conf_raw = None
                _conf_match = _re_dec.search(r'conf=([\d.]+)', notes)
                if _conf_match:
                    try:
                        _conf_raw = float(_conf_match.group(1))
                    except ValueError:
                        pass
                if _conf_raw is not None:
                    _conf_pct = int(_conf_raw * 100) if _conf_raw <= 1.0 else int(_conf_raw)
                    if _conf_pct >= 80:
                        _conf_col = "#16a34a"
                    elif _conf_pct >= 60:
                        _conf_col = "#ca8a04"
                    else:
                        _conf_col = "#dc2626"
                    _conf_html = (
                        f'<span style="font-size:0.73rem;font-weight:700;color:{_conf_col}">'
                        f'Confidenza: {_conf_pct}%</span>'
                    )
                else:
                    _conf_html = ""

                # ── Reason bullets ─────────────────────────────────────────────
                _reasons = _parse_dec_reasons(notes, factors, pct)
                _reasons_html = "".join(
                    f'<div class="dec-reason">'
                    f'<span class="dec-reason-icon">{_ic}</span>'
                    f'<span>{_html.escape(_tx)}</span>'
                    f'</div>'
                    for _ic, _tx in _reasons
                )

                # ── Assemble card ──────────────────────────────────────────────
                _card_html = (
                    f'<div class="dec-card {_card_mod}">'

                    # Header: property name + status badge
                    f'<div style="display:flex;justify-content:space-between;'
                    f'align-items:flex-start;margin-bottom:2px">'
                    f'<div class="dec-prop-name">{pname}</div>'
                    f'{_status_html}'
                    f'</div>'

                    # Action tag (price up / down / flat)
                    f'<div class="dec-action-tag {_action_mod}">{_action_txt}</div>'

                    # Price display
                    f'{_price_block}'

                    # Signal pills + Reason section
                    f'{_sig_pills_html}'
                    f'<div class="dec-reasons-hdr">Perché PricePilot ha modificato il prezzo</div>'
                    f'{_reasons_html}'

                    # Footer with timestamp + confidence
                    f'<div class="dec-footer">'
                    f'<span>🕐 {_ts_display}</span>'
                    f'{_conf_html}'
                    f'</div>'

                    f'</div>'
                )

                with _dec_cols[_di % 2]:
                    st.markdown(_card_html, unsafe_allow_html=True)

    except Exception as exc:
        st.warning(f"Feed decisioni non disponibile: {exc}")

    st.markdown("---")

    # ══════════════════════════════════════════════════════════════════════════════
    # PART 5 — ALERT E NOTIFICHE
    # ══════════════════════════════════════════════════════════════════════════════
    st.markdown('<div class="section-title">🔔 Alert e Opportunità</div>', unsafe_allow_html=True)

    alerts = []

    # Alert: pending approvals
    try:
        pending = get_pending_approvals(account_id=account_id)
        if pending:
            _n_p = len(pending)
            alerts.append({
                "type": "yellow",
                "icon": "⏳",
                "title": f"{_n_p} decisione{'i' if _n_p > 1 else ''} di pricing in attesa di approvazione",
                "desc": "Vai al tab Decisioni per verificare e approvare.",
            })
    except Exception:
        pending = []

    # Alert: high-impact events in the next 7 days
    try:
        from pricepilot.data_sources.events import get_upcoming_events
        upcoming_evts = get_upcoming_events(days=7)
        high_evts = [e for e in upcoming_evts if e.get("impact_level") == "high"]
        if high_evts:
            _evt_names = ", ".join(e["name"] for e in high_evts[:2])
            alerts.append({
                "type": "red",
                "icon": "🎉",
                "title": f"Evento ad alta domanda nella tua zona: {_evt_names}",
                "desc": "I prezzi nella tua zona stanno salendo. PricePilot si adatterà automaticamente.",
            })
        elif upcoming_evts:
            _e0 = upcoming_evts[0]
            alerts.append({
                "type": "blue",
                "icon": "📌",
                "title": f"Evento in arrivo: {_e0['name']}",
                "desc": f"Data: {_e0['date']}  ·  Impatto: {_e0['impact_level'].capitalize()}",
            })
    except Exception:
        pass

    # Alert: weekend in arrivo (giovedì o prima)
    days_to_weekend = (4 - today.weekday()) % 7
    if days_to_weekend <= 2:
        alerts.append({
            "type": "blue",
            "icon": "🏖️",
            "title": "Weekend in arrivo",
            "desc": "La domanda aumenta del 15–25% → alza il prezzo ora e guadagna di più.",
        })

    # Alert: occupazione bassa
    if avg_all_occ < 0.40:
        alerts.append({
            "type": "red",
            "icon": "📉",
            "title": f"Occupazione sotto obiettivo ({int(avg_all_occ * 100)}%)",
            "desc": "L'occupazione è bassa → abbassa il prezzo per evitare notti vuote.",
        })

    # Alert: prezzo molto sopra mercato (rischio di perdere prenotazioni)
    try:
        if pos_vs_market > 15:
            alerts.append({
                "type": "yellow",
                "icon": "⚠️",
                "title": f"Il tuo prezzo è {pos_vs_market:.0f}% sopra la media di mercato",
                "desc": f"Sei sopra mercato del {pos_vs_market:.0f}% → rischi di perdere prenotazioni.",
            })
    except Exception:
        pass

    # Alert: opportunità di ricavo (prezzo significativamente sotto mercato)
    try:
        if pos_vs_market < -10:
            alerts.append({
                "type": "green",
                "icon": "⚡",
                "title": f"Sei sotto mercato del {abs(pos_vs_market):.0f}% — puoi guadagnare di più",
                "desc": f"Sei sotto mercato del {abs(pos_vs_market):.0f}% → aumenta il prezzo ora e guadagna di più.",
            })
    except Exception:
        pass

    if not alerts:
        st.markdown(
            '<div class="alert-green">'
            '✅ <b>Tutto ok</b> — PricePilot sta funzionando correttamente. '
            'Nessun problema o opportunità rilevata al momento.'
            '</div>',
            unsafe_allow_html=True,
        )
    else:
        for alert in alerts:
            st.markdown(
                f'<div class="alert-{alert["type"]}">'
                f'<b>{alert["icon"]} {alert["title"]}</b><br>'
                f'<span style="font-size:0.88rem;color:#555">{alert["desc"]}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

    # Approvazioni in sospeso con pulsanti
    if pending:
        st.markdown("##### ⏳ Approva o rifiuta le decisioni in sospeso")
        prop_map_a = {p["id"]: p["name"] for p in props}
        for item in pending:
            pname = prop_map_a.get(item["property_id"], f"#{item['property_id']}")
            pct   = (item["new_price"] - item["old_price"]) / max(item["old_price"], 1) * 100
            arrow = "▲" if pct > 0 else "▼"
            occ   = item.get("occupancy") or 0
            ts    = (item.get("timestamp") or "")[:10]
            notes = item.get("notes", "")

            _conf_v = None
            _evt_v  = ""
            for _xp in notes.split("|"):
                _xp = _xp.strip()
                if _xp.startswith("conf="):
                    try: _conf_v = float(_xp.split("=", 1)[1])
                    except Exception: pass
                elif _xp.startswith("event="):
                    _evt_v = _xp.split("=", 1)[1].strip()

            col_info, col_approve, col_reject = st.columns([5, 1, 1])
            with col_info:
                pct_col = "#155724" if pct > 0 else "#721c24"
                st.markdown(
                    f"<div style='font-size:0.95rem'>"
                    f"<b>{pname}</b> &nbsp; <code>{ts}</code> &nbsp;|&nbsp; "
                    f"€{item['old_price']:.2f} → "
                    f"<span style='color:{pct_col};font-weight:700'>€{item['new_price']:.2f}</span> "
                    f"({arrow} {abs(pct):.1f}%) &nbsp;|&nbsp; Occ: {int(occ*100)}%"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                st.markdown(
                    _build_why_block(
                        notes=notes,
                        factors=item.get("factors") or "",
                        mpi=item.get("mpi"),
                        conf=_conf_v,
                        has_event=bool(_evt_v and _evt_v != "none"),
                        event_name=_evt_v,
                        occupancy=occ,
                    ),
                    unsafe_allow_html=True,
                )
            with col_approve:
                if st.button("✅ SI", key=f"home_app_{item['id']}",
                             use_container_width=True, type="primary"):
                    result = approve_decision(item["id"], account_id=account_id)
                    st.toast("Approvato. Aggiorna manualmente il prezzo sul canale.", icon="✅")
                    st.rerun()
            with col_reject:
                if st.button("❌ NO", key=f"home_rej_{item['id']}",
                             use_container_width=True):
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE decision_log SET applied=0, "
                            "decision=decision||' [REJECTED]' WHERE id=? AND account_id=?",
                            (item["id"], account_id)
                        )
                    st.toast("Rifiutato.", icon="❌")
                    st.rerun()
            st.divider()


# ═════════════════════════════════════════════════════════════════════════════
# HELPER: WHY THIS PRICE BLOCK (Explainable Pricing)
# ═════════════════════════════════════════════════════════════════════════════

def _build_why_block(
    notes: str = "",
    factors: str = "",          # JSON string or ""
    safety_note: str = "",
    mpi: float = None,
    conf: float = None,         # 0.0–1.0
    has_event: bool = False,
    event_name: str = "",
    is_weekend: bool = False,
    occupancy: float = None,
) -> str:
    """
    Ritorna un blocco HTML 'Perché questo prezzo' con:
    - lista punti chiave estratti da notes/factors
    - badge Confidenza (HIGH/MEDIUM/LOW)
    - badge MPI con colore semantico
    Retrocompatibile: accetta parametri mancanti (None/"")
    """
    import json as _json
    import re as _re

    bullets = []

    def _money(value: str) -> str:
        try:
            return f"€{float(value):.0f}"
        except Exception:
            return str(value)

    def _safety_bullets(raw_note: str) -> list:
        if not raw_note or str(raw_note).strip().lower() == "ok":
            return []
        readable = []
        for chunk in [p.strip() for p in str(raw_note).split("|") if p.strip()]:
            m = _re.search(r"clamped_up\s*\(([\d.]+)->([\d.]+),\s*max \+([\d.]+)%\)", chunk)
            if m:
                readable.append(
                    "Prezzo limitato per sicurezza: PricePilot avrebbe aumentato di piu, "
                    f"ma applica massimo +{m.group(3)}% per singolo aggiornamento "
                    f"({_money(m.group(1))} -> {_money(m.group(2))})."
                )
                continue
            m = _re.search(r"clamped_down\s*\(([\d.]+)->([\d.]+),\s*max -([\d.]+)%\)", chunk)
            if m:
                readable.append(
                    "Prezzo limitato per sicurezza: PricePilot avrebbe abbassato di piu, "
                    f"ma applica massimo -{m.group(3)}% per singolo aggiornamento "
                    f"({_money(m.group(1))} -> {_money(m.group(2))})."
                )
                continue
            m = _re.search(r"dynamic_floor=([\d.]+)", chunk)
            if m:
                readable.append(
                    "Prezzo minimo dinamico applicato: in giorni con piu domanda "
                    f"PricePilot non scende sotto {_money(m.group(1))}."
                )
                continue
            if chunk.startswith("last_minute"):
                readable.append("Regola last minute applicata: vicino alla data PricePilot evita sconti troppo aggressivi.")
            elif chunk.startswith("early_booking"):
                readable.append("Regola prenotazioni future applicata: sulle date lontane PricePilot evita ribassi troppo forti.")
            elif chunk.startswith("break_even"):
                readable.append("Prezzo minimo operativo rispettato: il prezzo non scende sotto i costi impostati.")
            else:
                readable.append("Regola di sicurezza applicata per evitare variazioni troppo aggressive.")
        return readable

    # ── Fattori da breakdown (factors JSON) ───────────────────────────────
    _factor_labels = {
        "base":           ("🏠", "Prezzo base"),
        "start":          ("🏠", "Prezzo di partenza"),
        "seasonality":    ("📅", "Stagionalità"),
        "season_factor":  ("📅", "Stagionalita"),
        "weekend":        ("📆", "Bonus weekend"),
        "weekend_boost":  ("📆", "Weekend"),
        "event":          ("🎉", "Evento locale"),
        "event_boost":    ("🎉", "Evento locale"),
        "occupancy":      ("📊", "Occupazione"),
        "occupancy_boost": ("📊", "Occupazione alta"),
        "occupancy_discount": ("📊", "Occupazione bassa"),
        "competitor":     ("🏆", "Pressione competitor"),
        "strategy":       ("🎯", "Strategia pricing"),
        "demand":         ("📈", "Domanda"),
        "location":       ("📍", "Contesto zona"),
        "min_price":      ("🔒", "Prezzo minimo garantito"),
        "max_price":      ("🔒", "Prezzo massimo"),
    }
    if factors:
        try:
            bd = _json.loads(factors) if isinstance(factors, str) else factors
            for key, val in bd.items():
                if key == "safety":
                    safety_note = str(val or safety_note)
                    continue
                icon, label = _factor_labels.get(key, ("•", key.replace("_", " ").capitalize()))
                if isinstance(val, (int, float)):
                    sign = "+" if val >= 0 else ""
                    bullets.append(f"{icon} {label}: <b>{sign}{val:g}%</b>" if abs(val) < 100
                                   else f"{icon} {label}: <b>€{val:g}</b>")
                else:
                    clean_val = str(val)
                    if key == "occupancy" and clean_val.startswith("neutro"):
                        clean_val = clean_val.replace("neutro", "nella norma")
                    bullets.append(f"{icon} {label}: <b>{clean_val}</b>")
        except Exception:
            pass

    for safety_text in _safety_bullets(safety_note):
        bullets.append(f"🛡️ {safety_text}")

    # ── Fallback: punti da notes ───────────────────────────────────────────
    if not bullets and notes:
        parts = [p.strip() for p in notes.split("|") if p.strip()
                 and not p.strip().startswith("conf=")
                 and not p.strip().startswith("event=")]
        for p in parts:
            bullets.append(f"💡 {p}")

    # ── Segnali contestuali (event / weekend / occupancy) ──────────────────
    if has_event and event_name and event_name != "none":
        bullets.append(f"🎉 Evento: <b>{event_name}</b>")
    if is_weekend:
        bullets.append("📆 Weekend: domanda più alta")
    if occupancy is not None:
        occ_pct = int(occupancy * 100) if occupancy <= 1 else int(occupancy)
        if occ_pct >= 80:
            bullets.append(f"🔥 Occupazione alta: <b>{occ_pct}%</b>")
        elif occ_pct <= 30:
            bullets.append(f"📉 Occupazione bassa: <b>{occ_pct}%</b>")
        else:
            bullets.append(f"📊 Occupazione: <b>{occ_pct}%</b>")

    # ── Badge Confidenza ───────────────────────────────────────────────────
    conf_html = ""
    if conf is not None:
        conf_pct = int(conf * 100) if conf <= 1.0 else int(conf)
        if conf_pct >= 70:
            conf_label, conf_col, conf_bg = "ALTA", "#15803d", "#dcfce7"
        elif conf_pct >= 50:
            conf_label, conf_col, conf_bg = "MEDIA", "#b45309", "#fef3c7"
        else:
            conf_label, conf_col, conf_bg = "BASSA", "#dc2626", "#fee2e2"
        conf_html = (
            f'<span style="background:{conf_bg};color:{conf_col};'
            f'padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600;">'
            f'Confidenza: {conf_label} ({conf_pct}%)</span>'
        )

    # ── Badge MPI ──────────────────────────────────────────────────────────
    mpi_html = ""
    if mpi is not None:
        if mpi < 95:
            mc, mbg = "#15803d", "#dcfce7"
            mlabel = f"{100 - mpi:.0f}% sotto media competitor"
        elif mpi <= 105:
            mc, mbg, mlabel = "#1d4ed8", "#dbeafe", "In linea con i competitor"
        else:
            mc, mbg = "#b45309", "#fef3c7"
            mlabel = f"{mpi - 100:.0f}% sopra media competitor"
        mpi_html = (
            f'<span style="background:{mbg};color:{mc};'
            f'padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600;">'
            f'Posizione mercato: {mlabel}</span>'
        )

    # ── Assembla HTML ──────────────────────────────────────────────────────
    bullets_html = "".join(
        f'<li style="margin:2px 0;font-size:13px;">{b}</li>' for b in bullets
    ) if bullets else '<li style="margin:2px 0;font-size:13px;color:#6b7280;">Nessun dettaglio disponibile</li>'

    badges = " ".join(filter(None, [conf_html, mpi_html]))

    return f"""
<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px;margin:6px 0;">
  <div style="font-size:12px;font-weight:600;color:#475569;margin-bottom:6px;">🔍 Perché questo prezzo</div>
  <ul style="margin:0;padding-left:18px;">{bullets_html}</ul>
  {f'<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;">{badges}</div>' if badges else ''}
</div>"""


# ═════════════════════════════════════════════════════════════════════════════
# TAB: STORICO & ANALISI  (tab_pricing removed – pricing is automatic)
# ═════════════════════════════════════════════════════════════════════════════

def tab_pricing(cfg: dict):
    """Pricing Strategy configuration page — UI only, no engine logic modified."""

    st.markdown('<div class="section-title">🎯 Strategia di Pricing</div>', unsafe_allow_html=True)
    st.caption("Configura come PricePilot ottimizza i prezzi per ogni tua proprietà.")

    # ── Property selector ──────────────────────────────────────────────────────
    _ps_props = list_properties(account_id=current_account_id())
    if not _ps_props:
        st.info("Nessuna proprietà configurata. Vai al tab **🏠 Home** per aggiungere la tua prima proprietà.")
        return

    _ps_ids    = [p["id"]   for p in _ps_props]
    _active_id = st.session_state.get("active_prop_id")
    if _active_id not in _ps_ids:
        _active_id = _ps_ids[0]
        st.session_state["active_prop_id"] = _active_id

    _sel_prop = next(p for p in _ps_props if p["id"] == _active_id)
    _sel_id   = _sel_prop["id"]
    apply_pending_property_pricing_widget_reset(_sel_id, pricing_tab=True)
    st.caption(f"Proprieta attiva: {_sel_prop.get('name', '')}. Cambiala dalla sidebar.")

    # ── Current values from DB ─────────────────────────────────────────────────
    _cur_strategy = _sel_prop.get("strategy", "balanced")
    _cur_min_float, _cur_max_float = get_synced_price_limits(_sel_prop, cfg)
    _cur_min      = int(_cur_min_float)
    _cur_max      = int(_cur_max_float)
    _pricing_price_key_suffix = _price_limit_widget_suffix(_sel_prop, cfg)

    # Demand sensitivity + weekend boost live in session_state (UI-only preferences)
    _sens_key      = f"ps_demand_sens_{_sel_id}"
    _boost_key     = f"ps_weekend_boost_{_sel_id}"
    _cur_sens      = st.session_state.get(_sens_key, 5)
    _cur_boost     = st.session_state.get(_boost_key, 20)

    _pricing_flash = st.session_state.pop(f"pp_pricing_price_flash_{_sel_id}", None)
    if _pricing_flash:
        st.success(_pricing_flash)

    st.markdown("---")

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 1 — PRICING STRATEGY
    # ══════════════════════════════════════════════════════════════════════════
    st.markdown(
        '<div class="ps-section-title">Strategia di pricing</div>'
        '<div class="ps-section-desc">'
        'Scegli quanto aggressivamente PricePilot massimizza i ricavi. '
        'Puoi cambiarlo in qualsiasi momento.'
        '</div>',
        unsafe_allow_html=True,
    )

    _strat_defs = [
        (
            "conservative", "🛡️", "Conservativa",
            "Priorità alla stabilità. Aumenti prudenti e massima sicurezza nelle prenotazioni. "
            "Ideale per chi preferisce alta occupazione rispetto al ricavo massimo.",
        ),
        (
            "balanced", "⚖️", "Bilanciata",
            "Il meglio dei due mondi. Si adatta alla domanda proteggendo l'occupazione. "
            "Consigliata per la maggior parte delle proprietà.",
        ),
        (
            "aggressive", "🚀", "Aggressiva",
            "Massimizzazione dei ricavi. Prezzi più alti nei periodi di alta domanda. "
            "Ideale per annunci premium in mercati ad alta richiesta.",
        ),
    ]

    _sc1, _sc2, _sc3 = st.columns(3)
    _new_strategy = _cur_strategy  # updated by button clicks below

    for _col, (_skey, _ico, _slbl, _sdesc) in zip([_sc1, _sc2, _sc3], _strat_defs):
        _active = _cur_strategy == _skey
        _border = "2px solid #667eea" if _active else "2px solid #e2e8f0"
        _bg     = "background:#f0f0ff;" if _active else "background:white;"
        _lbl_st = "color:#667eea;font-weight:900;" if _active else "color:#1e293b;font-weight:700;"
        _check  = "  ✓" if _active else ""
        with _col:
            st.markdown(
                f'<div class="strat-card" style="border:{_border};{_bg}">'
                f'<div style="font-size:2rem;margin-bottom:10px">{_ico}</div>'
                f'<div style="font-size:0.98rem;{_lbl_st}">{_slbl}{_check}</div>'
                f'<div style="font-size:0.79rem;color:#6b7280;margin-top:8px;line-height:1.5">'
                f'{_sdesc}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
            if st.button(
                "Strategia attiva" if _active else f"Passa a {_slbl}",
                key=f"ps_btn_{_skey}_{_sel_id}",
                disabled=_active,
                use_container_width=True,
            ):
                _new_strategy = _skey

    # Apply strategy change immediately (no Save needed for strategy)
    if _new_strategy != _cur_strategy:
        try:
            update_property(_sel_id, {"strategy": _new_strategy})
            st.session_state["active_prop_id"] = _sel_id
            queue_property_pricing_widget_reset(_sel_id, sidebar=True, pricing_tab=False)
            st.cache_data.clear()
            st.rerun()
        except Exception as _se:
            st.error(f"Impossibile salvare la strategia: {_se}")

    st.markdown('<div style="height:4px"></div>', unsafe_allow_html=True)
    st.markdown("---")

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 2 — GUARDRAILS + SENSITIVITY (2-column layout)
    # ══════════════════════════════════════════════════════════════════════════
    _gcol, _scol = st.columns(2, gap="large")

    # ── Left: Price guardrails ─────────────────────────────────────────────────
    with _gcol:
        st.markdown(
            '<div class="ps-section-title">💰 Fasce di prezzo</div>'
            '<div class="ps-section-desc">'
            'PricePilot non uscirà mai da questi limiti assoluti, '
            'indipendentemente dalle condizioni di mercato.'
            '</div>',
            unsafe_allow_html=True,
        )
        _new_min = st.number_input(
            "Prezzo minimo (€/notte)",
            min_value=10, max_value=9990,
            value=_cur_min, step=5,
            key=f"ps_min_{_pricing_price_key_suffix}",
            help="Il prezzo non scenderà mai sotto questo valore.",
        )
        _new_max = st.number_input(
            "Prezzo massimo (€/notte)",
            min_value=11, max_value=9999,
            value=_cur_max, step=5,
            key=f"ps_max_{_pricing_price_key_suffix}",
            help="Il prezzo non supererà mai questo valore.",
        )
        if int(_new_min) >= int(_new_max):
            st.warning("⚠️ Il prezzo minimo deve essere inferiore al massimo.")
        else:
            st.markdown(
                f'<div class="ps-guardrail-note">'
                f'PricePilot opererà nel range '
                f'<strong>€{int(_new_min)} – €{int(_new_max)}</strong> a notte.'
                f'</div>',
                unsafe_allow_html=True,
            )

    # ── Right: Demand sensitivity + weekend boost ──────────────────────────────
    with _scol:
        st.markdown(
            '<div class="ps-section-title">📡 Sensibilità alla domanda</div>'
            '<div class="ps-section-desc">'
            'Controlla quanto il motore reagisce ai segnali di domanda '
            'e alle variazioni dei prezzi dei competitor.'
            '</div>',
            unsafe_allow_html=True,
        )
        _new_sens = st.slider(
            "Sensibilità alla domanda",
            min_value=1, max_value=10,
            value=_cur_sens, step=1,
            key=f"ps_sens_{_sel_id}",
            help="1 = molto stabile (piccole variazioni) · 10 = molto reattivo (variazioni ampie)",
        )
        _sens_labels = {
            1: "Molto stabile", 2: "Stabile", 3: "Leggermente stabile",
            4: "Moderato", 5: "Bilanciato",
            6: "Leggermente reattivo", 7: "Reattivo", 8: "Molto reattivo",
            9: "Altamente reattivo", 10: "Massima reattività",
        }
        st.caption(f"Livello {_new_sens} — {_sens_labels.get(_new_sens, '')}")

        st.markdown('<div style="height:12px"></div>', unsafe_allow_html=True)

        st.markdown(
            '<div class="ps-section-title">📅 Incremento prezzo weekend</div>'
            '<div class="ps-section-desc">'
            'Aumenta automaticamente i prezzi nei venerdì e sabato.'
            '</div>',
            unsafe_allow_html=True,
        )
        _new_boost = st.slider(
            "Incremento weekend (%)",
            min_value=0, max_value=50,
            value=_cur_boost, step=5,
            key=f"ps_boost_{_sel_id}",
            help="Percentuale extra aggiunta automaticamente nelle notti di venerdì e sabato.",
        )
        if _new_boost == 0:
            st.caption("Nessun aumento applicato nel weekend.")
        else:
            st.caption(f"I prezzi del weekend saranno {_new_boost}% più alti dei giorni feriali.")

    st.markdown('<div style="height:16px"></div>', unsafe_allow_html=True)

    # ── Save button ────────────────────────────────────────────────────────────
    _btn_disabled = int(_new_min) >= int(_new_max)
    _save_col, _, _ = st.columns([1, 1, 1])
    with _save_col:
        if st.button(
            "💾 Salva impostazioni",
            type="primary",
            key=f"ps_save_{_sel_id}",
            disabled=_btn_disabled,
            use_container_width=True,
        ):
            try:
                save_synced_price_limits(
                    _sel_prop,
                    float(_new_min),
                    float(_new_max),
                    strategy=_new_strategy,
                    reset_sidebar=True,
                    reset_pricing_tab=False,
                )
                # Persist session-only preferences
                st.session_state[_sens_key]  = _new_sens
                st.session_state[_boost_key] = _new_boost
                st.session_state[f"pp_pricing_price_flash_{_sel_id}"] = (
                    "Impostazioni salvate e limiti prezzo sincronizzati."
                )
                st.rerun()
            except Exception as _se:
                st.error(f"Salvataggio non riuscito: {_se}")



# ═════════════════════════════════════════════════════════════════════════════
# TAB: STORICO & ANALISI
# ═════════════════════════════════════════════════════════════════════════════

def tab_analytics(cfg: dict):
    account_id = current_account_id()
    st.markdown('<div class="section-title">📈 Quanto hai guadagnato — e quanto avresti potuto</div>',
                unsafe_allow_html=True)

    df = load_decisions_df(limit=500, account_id=account_id)

    if df.empty:
        st.info("📭 Nessun dato storico disponibile. I prezzi vengono registrati automaticamente dal motore PricePilot.")
        return

    # ── KPI ──────────────────────────────────────────────────────────────────
    kpis = compute_kpis(df)
    rev  = estimate_revenue(df)

    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("🤖 Ottimizzazioni AI", kpis["total_decisions"],
              help="Quante volte il motore ha calcolato un prezzo ottimale")
    k2.metric("💰 Ricavo medio/notte", f"€{kpis['avg_price']:.2f}",
              help="Il tuo ricavo medio per notte nel periodo selezionato")
    k3.metric("🚀 Picco di ricavo",    f"€{kpis['max_price']:.2f}",
              help="Il prezzo più alto applicato — catturato in un momento ad alta domanda")
    k4.metric("📉 Prezzo minimo",      f"€{kpis['min_price']:.2f}",
              help="Il prezzo più basso applicato — verifica se era necessario")
    k5.metric("📈 Ottimizzazione media", f"{kpis['avg_pct_change']:+.1f}%",
              help="Di quanto il motore ha alzato o abbassato i prezzi in media")
    k6.metric("🏠 Domanda di prenotazione", f"{kpis['avg_occupancy']:.1f}%",
              help="Tasso di occupazione medio — più alto = più prenotazioni")

    # ── Filtri date ───────────────────────────────────────────────────────────
    st.markdown("---")
    fcol1, fcol2 = st.columns(2)
    with fcol1:
        min_date = df["date"].min().date()
        max_date = df["date"].max().date()
        date_range = st.date_input("Periodo", value=(min_date, max_date),
                                   min_value=min_date, max_value=max_date)
    with fcol2:
        strategy_filter = st.multiselect(
            "Filtra per strategia",
            options=df["strategy"].dropna().unique().tolist(),
            default=df["strategy"].dropna().unique().tolist(),
        )

    # Applica filtri
    mask = pd.Series([True] * len(df))
    if len(date_range) == 2:
        mask &= (df["date"].dt.date >= date_range[0]) & (df["date"].dt.date <= date_range[1])
    if strategy_filter:
        mask &= df["strategy"].isin(strategy_filter)
    df_f = df[mask].copy()

    if df_f.empty:
        st.warning("Nessun dato nel periodo selezionato.")
        return

    # ── Indicatori di Qualità Pricing (MPI + Confidenza) ─────────────────────
    st.markdown('<div class="section-title">🎯 Qualità delle Decisioni di Pricing</div>',
                unsafe_allow_html=True)
    try:
        _v2_log = get_decision_log(property_id=None, limit=500, account_id=account_id)
    except Exception:
        _v2_log = []

    if _v2_log:
        # Raccogli MPI e confidenza dai log v2
        _mpis  = [d["mpi"] for d in _v2_log if d.get("mpi") is not None]
        _confs = []
        for _d2 in _v2_log:
            for _np2 in (_d2.get("notes") or "").split("|"):
                _np2 = _np2.strip()
                if _np2.startswith("conf="):
                    try:
                        _confs.append(float(_np2.split("=", 1)[1]))
                    except Exception:
                        pass
                    break

        _mpi_avg  = round(sum(_mpis) / len(_mpis), 1)  if _mpis  else None
        _conf_avg = round(sum(_confs) / len(_confs) * 100, 1) if _confs else None
        _n_high   = sum(1 for c in _confs if c >= 0.70)
        _n_med    = sum(1 for c in _confs if 0.50 <= c < 0.70)
        _n_low    = sum(1 for c in _confs if c < 0.50)
        _mpi_lo   = sum(1 for m in _mpis if m < 95)
        _mpi_ok   = sum(1 for m in _mpis if 95 <= m <= 105)
        _mpi_hi   = sum(1 for m in _mpis if m > 105)
        _tot_c    = max(_n_high + _n_med + _n_low, 1)
        _tot_m    = max(len(_mpis), 1)

        iq1, iq2, iq3, iq4 = st.columns(4)

        # ── Card MPI medio ────────────────────────────────────────────────
        if _mpi_avg is not None:
            if _mpi_avg < 95:
                _mc, _ml = "#15803d", "Sotto mercato"
            elif _mpi_avg <= 105:
                _mc, _ml = "#1d4ed8", "In linea col mercato"
            else:
                _mc, _ml = "#b45309", "Sopra mercato"
            iq1.markdown(
                f'<div style="text-align:center;background:#f8fafc;border:1px solid #e2e8f0;'
                f'border-radius:10px;padding:16px 10px">'
                f'<div style="font-size:0.78rem;color:#64748b;margin-bottom:4px">📊 MPI Medio</div>'
                f'<div style="font-size:2rem;font-weight:900;color:{_mc}">{_mpi_avg:.0f}</div>'
                f'<div style="font-size:0.75rem;color:{_mc};font-weight:600">{_ml}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            iq1.caption("MPI non ancora disponibile")

        # ── Card Confidenza media ─────────────────────────────────────────
        if _conf_avg is not None:
            _cc2 = "#15803d" if _conf_avg >= 70 else ("#b45309" if _conf_avg >= 50 else "#dc2626")
            _cl2 = "Alta" if _conf_avg >= 70 else ("Media" if _conf_avg >= 50 else "Bassa")
            iq2.markdown(
                f'<div style="text-align:center;background:#f8fafc;border:1px solid #e2e8f0;'
                f'border-radius:10px;padding:16px 10px">'
                f'<div style="font-size:0.78rem;color:#64748b;margin-bottom:4px">🔮 Confidenza Media</div>'
                f'<div style="font-size:2rem;font-weight:900;color:{_cc2}">{_conf_avg:.0f}%</div>'
                f'<div style="font-size:0.75rem;color:{_cc2};font-weight:600">{_cl2}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            iq2.caption("Confidenza non ancora disponibile")

        # ── Card distribuzione confidenza ─────────────────────────────────
        iq3.markdown(
            f'<div style="background:#f8fafc;border:1px solid #e2e8f0;'
            f'border-radius:10px;padding:16px 14px">'
            f'<div style="font-size:0.78rem;color:#64748b;margin-bottom:8px;font-weight:600">'
            f'🔢 Distribuzione Confidenza</div>'
            f'<div style="font-size:0.82rem;margin:3px 0">'
            f'<span style="color:#15803d;font-weight:700">■ ALTA</span>&nbsp; '
            f'{_n_high} &nbsp;<span style="color:#94a3b8">({_n_high/_tot_c*100:.0f}%)</span></div>'
            f'<div style="font-size:0.82rem;margin:3px 0">'
            f'<span style="color:#b45309;font-weight:700">■ MEDIA</span> '
            f'{_n_med} &nbsp;<span style="color:#94a3b8">({_n_med/_tot_c*100:.0f}%)</span></div>'
            f'<div style="font-size:0.82rem;margin:3px 0">'
            f'<span style="color:#dc2626;font-weight:700">■ BASSA</span>&nbsp; '
            f'{_n_low} &nbsp;<span style="color:#94a3b8">({_n_low/_tot_c*100:.0f}%)</span></div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # ── Card distribuzione MPI ────────────────────────────────────────
        iq4.markdown(
            f'<div style="background:#f8fafc;border:1px solid #e2e8f0;'
            f'border-radius:10px;padding:16px 14px">'
            f'<div style="font-size:0.78rem;color:#64748b;margin-bottom:8px;font-weight:600">'
            f'📈 Distribuzione MPI</div>'
            f'<div style="font-size:0.82rem;margin:3px 0">'
            f'<span style="color:#15803d;font-weight:700">■ Sotto</span> '
            f'{_mpi_lo} &nbsp;<span style="color:#94a3b8">({_mpi_lo/_tot_m*100:.0f}%)</span></div>'
            f'<div style="font-size:0.82rem;margin:3px 0">'
            f'<span style="color:#1d4ed8;font-weight:700">■ In linea</span> '
            f'{_mpi_ok} &nbsp;<span style="color:#94a3b8">({_mpi_ok/_tot_m*100:.0f}%)</span></div>'
            f'<div style="font-size:0.82rem;margin:3px 0">'
            f'<span style="color:#b45309;font-weight:700">■ Sopra</span> '
            f'{_mpi_hi} &nbsp;<span style="color:#94a3b8">({_mpi_hi/_tot_m*100:.0f}%)</span></div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        st.caption(
            "💡 Gli indicatori MPI e Confidenza sono disponibili dopo le prime "
            "decisioni automatiche del motore v2."
        )

    st.markdown("---")

    # ── Grafico storico prezzi ────────────────────────────────────────────────
    st.markdown('<div class="section-title">📉 Il Tuo Prezzo vs Mercato</div>',
                unsafe_allow_html=True)

    daily = price_vs_market_df(df_f)
    ma    = rolling_avg_price(df_f, window=7)

    # ── Riepilogo decisione-focused sopra il grafico ──────────────────────────
    try:
        if not daily.empty and "market_price" in daily.columns and "our_price" in daily.columns:
            _days_below = int((daily["our_price"] < daily["market_price"]).sum())
            _days_above = int((daily["our_price"] > daily["market_price"]).sum())
            _avg_gap    = (daily["market_price"] - daily["our_price"]).mean()
            _est_loss   = max(0.0, _avg_gap) * _days_below
            _sc1, _sc2, _sc3 = st.columns(3)
            _sc1.metric(
                "📉 Giorni sotto mercato",
                f"{_days_below}",
                delta=f"su {len(daily)} giorni analizzati",
                delta_color="off",
            )
            _sc2.metric(
                "📈 Giorni sopra mercato",
                f"{_days_above}",
                delta="prezzi competitivi",
                delta_color="off",
            )
            _sc3.metric(
                "💸 Perdita stimata periodo",
                f"€{_est_loss:,.0f}",
                delta="giorni con prezzo basso vs mercato",
                delta_color="inverse",
            )
    except Exception:
        pass

    if not daily.empty:
        # ── Selezione difensiva colonne min/max mercato ───────────────────────
        if "market_max" in daily.columns:
            market_max_series = daily["market_max"]
        elif "competitor_max" in daily.columns:
            market_max_series = daily["competitor_max"]
        elif "market_price" in daily.columns:
            market_max_series = daily["market_price"]
        else:
            market_max_series = daily["our_price"]

        if "market_min" in daily.columns:
            market_min_series = daily["market_min"]
        elif "competitor_min" in daily.columns:
            market_min_series = daily["competitor_min"]
        elif "market_price" in daily.columns:
            market_min_series = daily["market_price"]
        else:
            market_min_series = daily["our_price"]

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=daily["date"], y=market_max_series,
            name="Max Mercato", line=dict(color="#e8e8e8", width=0),
            fill=None, showlegend=False,
        ))
        fig.add_trace(go.Scatter(
            x=daily["date"], y=market_min_series,
            name="Range Mercato", line=dict(color="#e8e8e8", width=0),
            fill="tonexty", fillcolor="rgba(102,126,234,0.1)",
            showlegend=True,
        ))
        fig.add_trace(go.Scatter(
            x=daily["date"], y=daily["market_price"],
            name="Media Mercato", line=dict(color="#9b59b6", dash="dot", width=2),
        ))
        fig.add_trace(go.Scatter(
            x=daily["date"], y=daily["our_price"],
            name="Nostro Prezzo", line=dict(color="#667eea", width=3),
            mode="lines+markers", marker=dict(size=6),
        ))
        if not ma.empty and f"ma_7d" in ma.columns:
            fig.add_trace(go.Scatter(
                x=ma["date"], y=ma["ma_7d"],
                name="Media Mobile 7gg", line=dict(color="#f39c12", dash="dash", width=2),
            ))
        fig.update_layout(
            title="Il tuo prezzo vs mercato — ogni giorno sotto la linea viola è ricavo perso",
            xaxis_title="Data", yaxis_title="Prezzo (€/notte)",
            height=420, hovermode="x unified",
            plot_bgcolor="white", paper_bgcolor="white",
            legend=dict(orientation="h", y=1.02),
        )
        st.plotly_chart(fig, use_container_width=True, config=PLOTLY_CONFIG)

    # ── Variazione % prezzi ───────────────────────────────────────────────────
    col_a, col_b = st.columns(2)

    with col_a:
        st.markdown('<div class="section-title">🔄 Variazioni Prezzo (%)</div>',
                    unsafe_allow_html=True)
        fig2 = px.bar(
            df_f.sort_values("date"),
            x="date", y="pct_change_display",
            color="pct_change_display",
            color_continuous_scale=["#e74c3c", "#f39c12", "#2ecc71"],
            labels={"pct_change_display": "Variazione %", "date": "Data"},
            title="Variazione % per decisione",
        )
        fig2.add_hline(y=0, line_dash="solid", line_color="black", line_width=1)
        fig2.update_layout(height=350, showlegend=False,
                           plot_bgcolor="white", paper_bgcolor="white")
        st.plotly_chart(fig2, use_container_width=True, config=PLOTLY_CONFIG)

    with col_b:
        st.markdown('<div class="section-title">🎉 Impatto Eventi</div>',
                    unsafe_allow_html=True)
        evt_df = event_impact_analysis(df_f)
        if not evt_df.empty:
            evt_df["Label"] = evt_df["has_event"].map({True: "Con evento", False: "Senza evento"})
            fig3 = px.bar(
                evt_df, x="Label", y="avg_price",
                color="Label",
                color_discrete_map={"Con evento": "#f39c12", "Senza evento": "#667eea"},
                title="Prezzo medio con/senza eventi",
                labels={"avg_price": "Prezzo Medio (€)"},
                text="avg_price",
            )
            fig3.update_traces(texttemplate="€%{text:.2f}", textposition="outside")
            fig3.update_layout(height=350, showlegend=False,
                               plot_bgcolor="white", paper_bgcolor="white")
            st.plotly_chart(fig3, use_container_width=True, config=PLOTLY_CONFIG)

    # ── Occupancy vs Prezzo scatter ───────────────────────────────────────────
    st.markdown('<div class="section-title">📊 Correlazione Occupazione → Prezzo</div>',
                unsafe_allow_html=True)
    occ_df = occupancy_price_correlation(df_f)
    if not occ_df.empty:
        occ_df["has_event"] = occ_df["event"].notna() & ~occ_df["event"].isin(["none", "0", ""])
        fig4 = px.scatter(
            occ_df, x="occupancy", y="new_price",
            color="has_event",
            color_discrete_map={True: "#f39c12", False: "#667eea"},
            labels={"occupancy": "Occupazione", "new_price": "Prezzo (€)",
                    "has_event": "Evento"},
            title="Occupazione vs Prezzo (arancione = con evento)",
            trendline="ols",
            opacity=0.7,
        )
        fig4.update_layout(height=380, plot_bgcolor="white", paper_bgcolor="white")
        st.plotly_chart(fig4, use_container_width=True, config=PLOTLY_CONFIG)

    # ── Breakdown per strategia ───────────────────────────────────────────────
    col_c, col_d = st.columns(2)

    with col_c:
        st.markdown('<div class="section-title">🎯 Uso Strategie</div>',
                    unsafe_allow_html=True)
        strat_df = strategy_breakdown(df_f)
        if not strat_df.empty:
            fig5 = px.pie(strat_df, names="strategy", values="count",
                          title="Distribuzione decisioni per strategia",
                          color_discrete_sequence=px.colors.qualitative.Pastel)
            fig5.update_layout(height=300)
            st.plotly_chart(fig5, use_container_width=True, config=PLOTLY_CONFIG)

    with col_d:
        st.markdown('<div class="section-title">💹 Revenue Stimata</div>',
                    unsafe_allow_html=True)
        if not df_f.empty:
            df_f2 = df_f.copy()
            df_f2["month"] = df_f2["date"].dt.to_period("M").astype(str)
            df_f2["est_revenue"] = df_f2["new_price"] * df_f2["occupancy"].fillna(0.7)
            monthly = df_f2.groupby("month")["est_revenue"].sum().reset_index()
            fig6 = px.bar(monthly, x="month", y="est_revenue",
                          title="Revenue stimata mensile",
                          labels={"month": "Mese", "est_revenue": "Revenue (€)"},
                          color_discrete_sequence=["#667eea"])
            fig6.update_layout(height=300, plot_bgcolor="white", paper_bgcolor="white")
            st.plotly_chart(fig6, use_container_width=True, config=PLOTLY_CONFIG)


# ═════════════════════════════════════════════════════════════════════════════
# TAB: LOG DECISIONI
# ═════════════════════════════════════════════════════════════════════════════

def _decision_note_values(notes: str) -> tuple[float | None, str]:
    conf = None
    event = ""
    for part in str(notes or "").split("|"):
        part = part.strip()
        if part.startswith("conf="):
            try:
                conf = float(part.split("=", 1)[1])
            except Exception:
                conf = None
        elif part.startswith("event="):
            event = part.split("=", 1)[1].strip()
    return conf, event


def _decision_flow_status(decision: dict) -> tuple[str, str, str, str, str]:
    text = str(decision.get("decision") or "")
    notes = str(decision.get("notes") or "")
    haystack = f"{text} {notes}".upper()
    mode = str(decision.get("mode") or "").lower()
    applied = int(decision.get("applied") or 0)

    if "REJECTED" in haystack:
        return (
            "rejected",
            "Rifiutata",
            "Decisione scartata: PricePilot non la sincronizzera.",
            "#991b1b",
            "#fef2f2",
        )
    if applied == 1 or "AUTO_APPLIED" in haystack:
        return (
            "applied",
            "Applicata",
            "Prezzo applicato sul canale collegato.",
            "#166534",
            "#f0fdf4",
        )
    if "APPROVED" in haystack:
        return (
            "approved_sync",
            "Approvata, da sincronizzare",
            "Approvata dall'utente: restera qui finche non colleghiamo channel manager/API.",
            "#92400e",
            "#fffbeb",
        )
    if "AUTO_RECOMMENDED" in haystack or mode == "auto":
        return (
            "approved_sync",
            "Pronta per sync OTA",
            "PricePilot l'avrebbe applicata in automatico, ma oggi manca ancora la sync reale.",
            "#92400e",
            "#fffbeb",
        )
    if "PENDING_APPROVAL" in haystack or mode == "approval":
        return (
            "pending",
            "Da approvare",
            "Serve conferma su Telegram o dalla dashboard.",
            "#92400e",
            "#fffbeb",
        )
    return (
        "suggested",
        "Consiglio generato",
        "Suggerimento motivato: l'utente aggiorna manualmente le OTA.",
        "#1e40af",
        "#eff6ff",
    )


def _decision_pct(old_price: float, new_price: float) -> float:
    if old_price <= 0:
        return 0.0
    return ((new_price - old_price) / old_price) * 100


def _decision_prop_name(decision: dict, prop_map: dict[int, str]) -> str:
    try:
        return prop_map.get(int(decision.get("property_id") or 0), "Proprieta")
    except Exception:
        return "Proprieta"


def _render_decision_flow_card(
    decision: dict,
    prop_map: dict[int, str],
    account_id: int,
    key_prefix: str,
):
    status_key, status_label, status_help, status_color, status_bg = _decision_flow_status(decision)
    decision_id = int(decision.get("id") or 0)
    old_price = float(decision.get("old_price") or 0)
    new_price = float(decision.get("new_price") or 0)
    pct = _decision_pct(old_price, new_price)
    arrow = "up" if pct > 1 else ("down" if pct < -1 else "flat")
    pct_color = "#166534" if pct > 0 else ("#991b1b" if pct < 0 else "#475569")
    prop_name = _html.escape(_decision_prop_name(decision, prop_map))
    date_label = _html.escape(str(decision.get("date") or decision.get("timestamp") or "")[:10] or "Data non disponibile")
    strategy = str(decision.get("strategy") or "").replace("_", " ").title()
    mode = str(decision.get("mode") or "advisory").lower()
    mode_label = {
        "advisory": "Manuale",
        "approval": "Approvazione",
        "auto": "Autopilot",
    }.get(mode, mode.title())
    market_avg = float(decision.get("market_avg") or decision.get("competitor_avg") or 0)
    market_html = (
        f'<span style="font-size:0.78rem;color:#64748b">Mercato: <b>EUR {market_avg:.0f}</b></span>'
        if market_avg else ""
    )
    occ = decision.get("occupancy")
    occ_html = ""
    if occ is not None:
        try:
            occ_val = float(occ)
            occ_pct = int(occ_val * 100) if occ_val <= 1 else int(occ_val)
            occ_html = f'<span style="font-size:0.78rem;color:#64748b">Occupazione: <b>{occ_pct}%</b></span>'
        except Exception:
            pass

    conf, event_name = _decision_note_values(str(decision.get("notes") or ""))
    is_wknd = False
    raw_date = str(decision.get("date") or "")
    if len(raw_date) == 10:
        try:
            is_wknd = date.fromisoformat(raw_date).weekday() >= 5
        except Exception:
            is_wknd = False

    st.markdown(
        f'<div style="background:{status_bg};border:1px solid #e2e8f0;'
        f'border-left:5px solid {status_color};border-radius:10px;'
        f'padding:14px 16px;margin:10px 0 6px;box-shadow:0 1px 3px rgba(15,23,42,.05)">'
        f'<div style="display:flex;justify-content:space-between;gap:14px;align-items:flex-start;flex-wrap:wrap">'
        f'<div style="min-width:260px;flex:1">'
        f'<div style="font-weight:900;color:#111827;font-size:1rem">{prop_name}</div>'
        f'<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:4px;color:#64748b;font-size:.78rem">'
        f'<span>{date_label}</span><span>{_html.escape(mode_label)}</span>'
        f'{f"<span>{_html.escape(strategy)}</span>" if strategy else ""}'
        f'</div></div>'
        f'<div style="text-align:right;min-width:180px">'
        f'<div style="font-size:.76rem;font-weight:900;color:{status_color};text-transform:uppercase">'
        f'{_html.escape(status_label)}</div>'
        f'<div style="font-size:.76rem;color:#64748b;max-width:260px">{_html.escape(status_help)}</div>'
        f'</div></div>'
        f'<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-top:12px">'
        f'<span style="color:#64748b">EUR {old_price:.2f}</span>'
        f'<span style="font-weight:900;color:{pct_color};font-size:1.12rem">EUR {new_price:.2f}</span>'
        f'<span style="font-weight:800;color:{pct_color};font-size:.88rem">{arrow} {pct:+.1f}%</span>'
        f'{market_html}{occ_html}'
        f'</div></div>',
        unsafe_allow_html=True,
    )

    st.markdown(
        _build_why_block(
            notes=str(decision.get("notes") or decision.get("decision") or ""),
            factors=decision.get("factors") or "",
            mpi=decision.get("mpi"),
            conf=conf,
            has_event=bool(event_name and event_name != "none"),
            event_name=event_name,
            is_weekend=is_wknd,
            occupancy=decision.get("occupancy"),
        ),
        unsafe_allow_html=True,
    )

    if status_key == "pending" and decision_id:
        col_yes, col_no, col_note = st.columns([1.1, 1.1, 4])
        with col_yes:
            if st.button("Approva", key=f"{key_prefix}_approve_{decision_id}", type="primary", use_container_width=True):
                from pricepilot.engine.decision_engine import approve_decision

                result = approve_decision(decision_id, account_id=account_id)
                if result.get("approved"):
                    st.toast("Decisione approvata. Ora resta in attesa di sync OTA.", icon="✅")
                else:
                    st.error(result.get("message", "Impossibile approvare questa decisione."))
                st.rerun()
        with col_no:
            if st.button("Rifiuta", key=f"{key_prefix}_reject_{decision_id}", use_container_width=True):
                from pricepilot.core.database import get_conn, update_calendar_status_for_decision

                with get_conn() as conn:
                    conn.execute(
                        "UPDATE decision_log SET applied=0, "
                        "decision=CASE WHEN instr(COALESCE(decision,''),'[REJECTED]') > 0 "
                        "THEN decision ELSE COALESCE(decision,'')||' [REJECTED]' END "
                        "WHERE id=? AND account_id=?",
                        (decision_id, account_id),
                    )
                try:
                    update_calendar_status_for_decision(
                        decision_log_id=decision_id,
                        status="rejected",
                        applied_price=None,
                        notes="Rifiutato dalla dashboard.",
                    )
                except Exception:
                    pass
                st.toast("Decisione rifiutata.", icon="❌")
                st.rerun()
        with col_note:
            st.caption("Nel piano Plus l'approvazione arriva anche da Telegram. Finche non colleghiamo il channel manager, l'approvazione non aggiorna ancora le OTA.")


def _render_decision_list(
    items: list[dict],
    prop_map: dict[int, str],
    account_id: int,
    key_prefix: str,
    empty_text: str,
):
    if not items:
        st.info(empty_text)
        return
    for item in items:
        _render_decision_flow_card(item, prop_map, account_id, key_prefix)


def _tab_decisions_v2(cfg: dict):
    account_id = current_account_id()
    st.markdown('<div class="section-title">Decisioni prezzi</div>', unsafe_allow_html=True)
    st.caption(
        "Qui segui il flusso operativo: consigli generati, approvazioni in attesa, "
        "prezzi approvati da sincronizzare, prezzi applicati e decisioni rifiutate."
    )

    props = list_properties(account_id=account_id)
    prop_map = {int(p["id"]): p["name"] for p in props}

    try:
        raw_log = get_decision_log(property_id=None, limit=500, account_id=account_id)
    except Exception:
        raw_log = []

    df_legacy = load_decisions_df(limit=500, account_id=account_id)

    if not raw_log and df_legacy.empty:
        st.info("Nessuna decisione ancora generata. Avvia un'analisi prezzi per popolare questa schermata.")
        return

    f1, f2 = st.columns([2, 1])
    prop_names = ["Tutte"] + [p["name"] for p in props]
    with f1:
        selected_prop = st.selectbox("Proprieta", prop_names, key="decision_flow_prop_filter")
    with f2:
        n_rows = st.slider("Decisioni da caricare", 10, 200, 60, key="decision_flow_limit")

    if raw_log:
        filtered = []
        for item in raw_log[:n_rows]:
            pname = _decision_prop_name(item, prop_map)
            if selected_prop == "Tutte" or pname == selected_prop:
                filtered.append(item)

        buckets = {
            "pending": [],
            "suggested": [],
            "approved_sync": [],
            "applied": [],
            "rejected": [],
        }
        for item in filtered:
            key = _decision_flow_status(item)[0]
            buckets.setdefault(key, []).append(item)

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Consigli", len(buckets["suggested"]))
        m2.metric("Da approvare", len(buckets["pending"]))
        m3.metric("Da sincronizzare", len(buckets["approved_sync"]))
        m4.metric("Applicate", len(buckets["applied"]))
        m5.metric("Rifiutate", len(buckets["rejected"]))

        st.markdown(
            '<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;'
            'padding:12px 14px;margin:10px 0 14px;color:#475569;font-size:.88rem">'
            '<b>Come leggerla:</b> Free genera consigli, Plus crea decisioni da approvare, '
            'Pro prepara l autopilot. Finche non colleghiamo channel manager/API, tutto cio che '
            'e approvato ma non applicato resta in "Da sincronizzare".'
            '</div>',
            unsafe_allow_html=True,
        )

        tab_pending, tab_suggested, tab_sync, tab_applied, tab_all = st.tabs([
            f"Da approvare ({len(buckets['pending'])})",
            f"Consigli ({len(buckets['suggested'])})",
            f"Da sincronizzare ({len(buckets['approved_sync'])})",
            f"Applicate ({len(buckets['applied'])})",
            f"Tutte ({len(filtered)})",
        ])

        with tab_pending:
            _render_decision_list(
                buckets["pending"], prop_map, account_id, "pending",
                "Nessuna decisione in attesa di approvazione.",
            )
        with tab_suggested:
            _render_decision_list(
                buckets["suggested"], prop_map, account_id, "suggested",
                "Nessun consiglio manuale al momento.",
            )
        with tab_sync:
            _render_decision_list(
                buckets["approved_sync"], prop_map, account_id, "sync",
                "Nessuna decisione approvata in attesa di sincronizzazione OTA.",
            )
        with tab_applied:
            _render_decision_list(
                buckets["applied"], prop_map, account_id, "applied",
                "Nessun prezzo applicato tramite sync reale.",
            )
        with tab_all:
            for item in filtered:
                _render_decision_flow_card(item, prop_map, account_id, "all")
            if buckets["rejected"]:
                with st.expander(f"Decisioni rifiutate ({len(buckets['rejected'])})", expanded=False):
                    _render_decision_list(
                        buckets["rejected"], prop_map, account_id, "rejected",
                        "Nessuna decisione rifiutata.",
                    )
    else:
        st.warning("Sto usando lo storico legacy: le nuove decisioni avranno stati piu chiari.")
        df_show = df_legacy.sort_values("timestamp", ascending=False).head(n_rows)
        st.dataframe(df_show, use_container_width=True, hide_index=True)

    st.markdown("---")
    e1, e2 = st.columns(2)
    with e1:
        if st.button("Esporta CSV", use_container_width=True, key="decision_flow_export_csv"):
            path = export_csv(account_id=account_id)
            if path:
                st.success(f"Esportato: `{path}`")
    with e2:
        if st.button("Esporta JSON", use_container_width=True, key="decision_flow_export_json"):
            path = export_json(account_id=account_id)
            if path:
                st.success(f"Esportato: `{path}`")


def tab_decisions(cfg: dict):
    return _tab_decisions_v2(cfg)

    account_id = current_account_id()
    st.markdown('<div class="section-title">📋 Log Decisioni di Pricing</div>',
                unsafe_allow_html=True)
    st.caption("Storico completo di tutte le decisioni di pricing suggerite o applicate da PricePilot.")

    # ── Prova a caricare dal decision_log v2 (più ricco) ─────────────────────
    props = list_properties(account_id=account_id)
    prop_map = {p["id"]: p["name"] for p in props}

    try:
        raw_log = get_decision_log(property_id=None, limit=500, account_id=account_id)
    except Exception:
        raw_log = []

    # ── Fallback al df legacy se il log v2 è vuoto ────────────────────────────
    df_legacy = load_decisions_df(limit=500, account_id=account_id)
    use_v2 = len(raw_log) > 0

    if not use_v2 and df_legacy.empty:
        st.info("Nessuna decisione salvata. I prezzi vengono registrati automaticamente dal motore PricePilot.")
        return

    # ── Filtri ────────────────────────────────────────────────────────────────
    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        filter_prop = st.selectbox(
            "Proprietà",
            options=["Tutte"] + [p["name"] for p in props],
            key="dec_filter_prop",
        )
    with fc2:
        filter_status = st.selectbox(
            "Stato",
            ["Tutti", "✅ Applicato", "⏳ In attesa", "❌ Rifiutato", "💡 Suggerito"],
            key="dec_filter_status",
        )
    with fc3:
        n_rows = st.slider("Righe da mostrare", 10, 200, 50, key="dec_nrows")

    st.markdown("---")

    if use_v2:
        # ── Vista v2: log ricco con motivo e stato ────────────────────────────
        shown = 0
        for d in raw_log:
            if shown >= n_rows:
                break

            old_p   = float(d.get("old_price") or 0)
            new_p   = float(d.get("new_price") or 0)
            pct     = ((new_p - old_p) / max(old_p, 1)) * 100 if old_p else 0
            ts      = str(d.get("timestamp", ""))[:10]
            pname   = prop_map.get(d.get("property_id"), "—")
            notes   = str(d.get("notes", "") or d.get("decision", "") or "")
            applied = d.get("applied")

            # Filtro proprietà
            if filter_prop != "Tutte" and pname != filter_prop:
                continue

            # Status e badge
            if applied == 1:
                status_key  = "✅ Applicato"
                status_html = '<span class="badge-green">✅ Applicato</span>'
                row_bg      = "#f0fff4"
            elif "REJECTED" in notes.upper():
                status_key  = "❌ Rifiutato"
                status_html = '<span class="badge-red">❌ Rifiutato</span>'
                row_bg      = "#fff5f5"
            elif applied is None or applied == -1:
                status_key  = "⏳ In attesa"
                status_html = '<span class="badge-yellow">⏳ In attesa</span>'
                row_bg      = "#fffbeb"
            else:
                status_key  = "💡 Suggerito"
                status_html = '<span class="badge-blue">💡 Suggerito</span>'
                row_bg      = "#ebf8ff"

            # Filtro status
            if filter_status != "Tutti" and status_key != filter_status:
                continue

            # ── Estrai conf e event dai notes ──────────────────────────────
            _conf_val  = None
            _event_val = ""
            for _np in notes.split("|"):
                _np = _np.strip()
                if _np.startswith("conf="):
                    try: _conf_val = float(_np.split("=", 1)[1])
                    except Exception: pass
                elif _np.startswith("event="):
                    _event_val = _np.split("=", 1)[1].strip()

            reason_parts = [
                p.strip() for p in notes.split("|")
                if p.strip()
                and not p.strip().startswith("conf=")
                and not p.strip().startswith("event=")
            ]
            reason_display = " · ".join(reason_parts[:4]) if reason_parts else "—"

            # Freccia e colore variazione
            arrow = "▲" if pct > 1 else ("▼" if pct < -1 else "→")
            pct_color = "#155724" if pct > 0 else ("#721c24" if pct < 0 else "#555")

            # ── Nuovi campi arricchiti ─────────────────────────────────────
            _strat    = d.get("strategy")
            _mpi_val  = d.get("mpi")
            _factors  = d.get("factors")
            _comp_avg = d.get("competitor_avg")
            _date_str = d.get("date", ts)
            _occ      = d.get("occupancy")

            # Badge strategia
            _strat_badge_cls = {
                "conservative": "badge-blue",
                "balanced":     "badge-green",
                "aggressive":   "badge-yellow",
                "premium":      "badge-purple",
            }.get(str(_strat or ""), "badge-blue")
            strat_html = (
                f'<span class="{_strat_badge_cls}">{_strat}</span>'
                if _strat else ""
            )

            # Badge Confidenza
            conf_html = ""
            if _conf_val is not None:
                _cp = int(_conf_val * 100) if _conf_val <= 1.0 else int(_conf_val)
                if _cp >= 70:
                    _cc, _cbg = "#15803d", "#dcfce7"
                elif _cp >= 50:
                    _cc, _cbg = "#b45309", "#fef3c7"
                else:
                    _cc, _cbg = "#dc2626", "#fee2e2"
                conf_html = (
                    f'<span style="background:{_cbg};color:{_cc};'
                    f'padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600;">'
                    f'Conf. {_cp}%</span>'
                )

            # Badge MPI
            if _mpi_val is not None:
                if _mpi_val < 95:
                    _mc, _mbg, _ml = "#15803d", "#dcfce7", f"MPI {_mpi_val:.0f} ↓"
                elif _mpi_val > 105:
                    _mc, _mbg, _ml = "#b45309", "#fef3c7", f"MPI {_mpi_val:.0f} ↑"
                else:
                    _mc, _mbg, _ml = "#1d4ed8", "#dbeafe", f"MPI {_mpi_val:.0f} ≈"
                mpi_html = (
                    f'<span style="background:{_mbg};color:{_mc};'
                    f'padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600;">'
                    f'{_ml}</span>'
                )
            else:
                mpi_html = ""

            comp_html = (
                f'<span style="font-size:0.78rem;color:#888;margin-left:8px">'
                f'Media competitor: <b>€{_comp_avg:.0f}</b></span>'
            ) if _comp_avg else ""

            st.markdown(
                f'<div style="background:{row_bg};border:1px solid #e8e8e8;'
                f'border-radius:10px;padding:14px 18px;margin-bottom:4px;'
                f'box-shadow:0 1px 4px rgba(0,0,0,.04)">'
                f'<div style="display:flex;align-items:flex-start;justify-content:space-between">'
                f'<div style="flex:1">'
                f'<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:6px">'
                f'<span style="font-weight:700;font-size:0.95rem">{pname}</span>'
                f'<span style="font-size:0.82rem;color:#888">📅 {_date_str}</span>'
                f'{strat_html}{conf_html}{mpi_html}'
                f'</div>'
                f'<div style="font-size:1rem;margin-bottom:4px">'
                f'<span style="color:#555">€{old_p:.2f}</span>'
                f'<span style="color:{pct_color};font-weight:700;margin:0 8px">'
                f'{arrow} €{new_p:.2f}</span>'
                f'<span style="color:{pct_color};font-size:0.85rem">'
                f'({arrow} {abs(pct):.1f}%)</span>'
                f'{comp_html}'
                f'</div>'
                f'</div>'
                f'<div style="margin-left:16px;flex-shrink:0;margin-top:2px">{status_html}</div>'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            # Why block — spiegazione strutturata (retrocompatibile)
            _is_wknd_dec = False
            if _date_str and len(str(_date_str)) == 10:
                try:
                    import datetime as _dt2
                    _is_wknd_dec = _dt2.date.fromisoformat(str(_date_str)).weekday() >= 5
                except Exception:
                    pass
            st.markdown(
                _build_why_block(
                    notes=notes,
                    factors=_factors or "",
                    mpi=_mpi_val,
                    conf=_conf_val,
                    has_event=bool(_event_val and _event_val != "none"),
                    event_name=_event_val,
                    is_weekend=_is_wknd_dec,
                    occupancy=_occ,
                ),
                unsafe_allow_html=True,
            )

            shown += 1

        if shown == 0:
            st.info("Nessuna decisione corrisponde ai filtri selezionati.")

    else:
        # ── Vista legacy: tabella dataframe ───────────────────────────────────
        mask = pd.Series([True] * len(df_legacy))
        df_show = df_legacy[mask].sort_values("timestamp", ascending=False).head(n_rows)

        display_cols = {
            "date":               "📅 Data",
            "old_price":          "Prezzo Pre (€)",
            "new_price":          "Prezzo Post (€)",
            "pct_change_display": "Variaz. (%)",
            "market_price":       "Media Mercato (€)",
            "occupancy":          "Occupazione",
            "event":              "Evento",
            "strategy":           "Strategia",
        }
        avail = [c for c in display_cols if c in df_show.columns]
        df_disp = df_show[avail].rename(columns=display_cols).copy()
        if "Occupazione" in df_disp.columns:
            df_disp["Occupazione"] = df_disp["Occupazione"].apply(
                lambda x: f"{x*100:.0f}%" if pd.notna(x) else "—"
            )
        st.dataframe(df_disp, use_container_width=True, hide_index=True)

    # ── Export ────────────────────────────────────────────────────────────────
    st.markdown("---")
    ec1, ec2 = st.columns(2)
    with ec1:
        if st.button("⬇️ Esporta CSV", use_container_width=True):
            path = export_csv(account_id=account_id)
            if path:
                st.success(f"✅ Esportato: `{path}`")
    with ec2:
        if st.button("⬇️ Esporta JSON", use_container_width=True):
            path = export_json(account_id=account_id)
            if path:
                st.success(f"✅ Esportato: `{path}`")


# ═════════════════════════════════════════════════════════════════════════════
# TAB: EVENTI
# ═════════════════════════════════════════════════════════════════════════════

def tab_events():
    st.markdown('<div class="section-title">🎉 Prossimi Eventi & Impatto Prezzi</div>',
                unsafe_allow_html=True)

    days_ahead = st.slider("Finestra eventi (giorni)", 14, 90, 30)
    events = get_upcoming_events(days=days_ahead)

    if not events:
        st.info("Nessun evento nei prossimi giorni.")
    else:
        impact_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        for e in events:
            icon = impact_icon.get(e["impact_level"], "⚪")
            boost_map = {"high": "+28-35%", "medium": "+12-20%", "low": "+5-10%"}
            boost = boost_map.get(e["impact_level"], "variabile")
            st.markdown(f"""
            **{icon} {e['name']}**
            &nbsp;|&nbsp; 📅 `{e['date']}`
            &nbsp;|&nbsp; Tipo: `{e['event_type']}`
            &nbsp;|&nbsp; Impatto: **{e['impact_level'].upper()}** ({boost})
            """)

    # Calendario mensile
    st.markdown('<div class="section-title">📅 Calendario Prezzi Consigliati (30 giorni)</div>',
                unsafe_allow_html=True)

    cfg = load_config()
    base = float(cfg.get("base_price", 80))
    occ  = 0.65
    rows = []
    for i in range(30):
        d = date.today() + timedelta(days=i)
        evt = get_event_for_date(d)
        comp_data = get_competitor_prices(base, d, event_to_string(evt), 6)
        prices    = [c["price"] for c in comp_data]
        res = calculate_price(base, prices, occ, event_to_string(evt),
                              cfg.get("strategy", "balanced"), cfg)
        rows.append({
            "Data":     d.isoformat(),
            "Giorno":   d.strftime("%a"),
            "Prezzo (€)": res["new_price"],
            "Mercato (€)": res["market_price"],
            "Evento":   evt["name"] if evt else "—",
            "Var. (%)": round(res["pct_change"] * 100, 1),
        })

    df_cal = pd.DataFrame(rows)
    st.dataframe(df_cal, use_container_width=True, hide_index=True)

    # Mini chart calendario
    fig = px.line(df_cal, x="Data", y="Prezzo (€)",
                  title="Prezzo consigliato prossimi 30 giorni",
                  markers=True, color_discrete_sequence=["#667eea"])
    fig.add_scatter(x=df_cal["Data"], y=df_cal["Mercato (€)"],
                    name="Media Mercato", line=dict(dash="dot", color="#9b59b6"))
    # Evidenzia eventi
    for _, row in df_cal[df_cal["Evento"] != "—"].iterrows():
        fig.add_vrect(x0=row["Data"], x1=row["Data"],
                      fillcolor="#f39c12", opacity=0.15, line_width=0)
    fig.update_layout(height=380, plot_bgcolor="white", paper_bgcolor="white",
                      hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True, config=PLOTLY_CONFIG)


# ═════════════════════════════════════════════════════════════════════════════
# TAB: PROPRIETÀ
# ═════════════════════════════════════════════════════════════════════════════

def _suggest_name_from_url(url: str, platform: str) -> str:
    """
    Suggerisce un nome proprietà estratto dall'URL del listing OTA.
    Usato nel form Step 1 per la auto-detection del nome.
    """
    import re
    try:
        url = url.strip()
        if not url:
            return ""
        # Airbnb: /rooms/{id} o /h/{slug}
        if "airbnb" in url or platform == "airbnb":
            m = re.search(r"/rooms/(\d+)", url)
            if m:
                return f"Airbnb Listing {m.group(1)}"
            m = re.search(r"/h/([^/?#]+)", url)
            if m:
                return m.group(1).replace("-", " ").title()
        # Booking.com: /hotel/{country}/{slug}.html
        if "booking.com" in url or platform == "booking":
            m = re.search(r"/hotel/[^/]+/([^/.?#]+)", url)
            if m:
                return m.group(1).replace("-", " ").title()
        # Vrbo: /listing/{id}
        if "vrbo" in url or platform == "vrbo":
            m = re.search(r"/listing/(\d+)", url)
            if m:
                return f"Vrbo Listing {m.group(1)}"
        # Generico: ultimo segmento path significativo
        from urllib.parse import urlparse
        path = urlparse(url).path
        segments = [s for s in path.rstrip("/").split("/") if s and not s.startswith("http")]
        if segments:
            candidate = segments[-1].split(".")[0]
            if len(candidate) >= 4:
                return candidate.replace("-", " ").replace("_", " ").title()
    except Exception:
        pass
    return ""


def tab_properties():
    """
    Gestione proprietà con:
    - Card visive per ogni proprietà
    - Quick Mode Switcher
    - Form guidato a 3 step: Info → Strategia → Prezzi
    """
    st.markdown('<div class="section-title">🏠 Le Tue Proprietà</div>',
                unsafe_allow_html=True)

    account_id = current_account_id()
    props = list_properties(account_id=account_id)

    if not props:
        st.caption("Configura la prima proprieta: dati essenziali, prezzi, piano e Telegram in pochi passaggi.")
        _tab_onboarding_v2("properties")
        return

    account = get_account(account_id) or {"id": account_id, "name": "La mia attivita", "plan": "free", "billing_status": "dev"}
    current_plan = account.get("plan", "free")
    plan_info = get_plan(current_plan)
    billing_status_labels = {
        "dev": "Demo locale",
        "trialing": "Prova gratuita",
        "active": "Attivo",
        "past_due": "Da verificare",
        "canceled": "Disattivato",
    }
    billing_status = str(account.get("billing_status", "dev")).lower()
    billing_label = billing_status_labels.get(billing_status, billing_status or "Non configurato")

    st.markdown('<div class="section-title">Account & Piano</div>', unsafe_allow_html=True)
    st.caption(
        "Qui controlli il piano, lo stato abbonamento, le notifiche e la configurazione minima "
        "prima delle integrazioni reali."
    )
    a1, a2, a3 = st.columns([2, 2, 1])
    with a1:
        account_name = st.text_input(
            "Nome attivita",
            value=account.get("name", "La mia attivita"),
            key="account_name_input",
        )
    with a2:
        _render_readonly_plan_box(current_plan, compact=True)
    with a3:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("Salva account", key="account_profile_save", use_container_width=True):
            update_account_profile(account_id, {"name": account_name.strip() or "La mia attivita"})
            for prop in props:
                update_property(prop["id"], {
                    **prop,
                    "plan": current_plan,
                    "sync_mode": effective_sync_mode(current_plan, prop.get("sync_mode")),
                })
            st.toast("Account aggiornato.", icon="✅")
            st.rerun()
        st.button("Cambia piano", key="account_plan_upgrade_disabled", use_container_width=True, disabled=True)

    st.caption(plan_info["description"])

    readiness = account_readiness(account_id)
    r1, r2, r3 = st.columns(3)
    with r1:
        st.metric("Configurazione pronta", f"{readiness['score']:.0f}%")
        st.caption("Quanto manca per usare PricePilot senza passaggi manuali.")
    with r2:
        st.metric("Cose da completare", len(readiness["blockers"]))
        st.caption("Elementi importanti non ancora configurati.")
    with r3:
        st.metric("Stato abbonamento", billing_label)
        st.caption("Demo locale = nessun pagamento reale collegato.")

    if billing_status == "dev":
        st.info(
            "Stai usando PricePilot in modalità demo locale: il piano viene mostrato come informazione account. "
            "Il cambio piano sarà collegato al billing reale."
        )

    with st.expander("Checklist configurazione", expanded=readiness["score"] < 80):
        st.caption("Questa lista dice cosa è pronto e cosa manca prima di collegare channel manager/API reali.")
        for check in readiness["checks"]:
            icon = "✅" if check["ok"] else ("⚠️" if check["required"] else "ℹ️")
            st.markdown(f"{icon} **{check['label']}** · {check.get('detail', '')}")

    prefs = get_notification_preferences(account_id=account_id)
    with st.expander("Notifiche Telegram e report", expanded=False):
        st.caption("Scegli quali messaggi PricePilot può inviare. Se Telegram non è collegato, le notifiche vengono registrate ma non inviate.")
        n1, n2, n3 = st.columns(3)
        with n1:
            telegram_enabled = st.checkbox(
                "Telegram attivo",
                value=bool(prefs.get("telegram_enabled", 1)),
                key="notif_telegram_enabled",
            )
        with n2:
            approval_alerts = st.checkbox(
                "Alert approvazione",
                value=bool(prefs.get("approval_alerts", 1)),
                key="notif_approval_alerts",
            )
        with n3:
            auto_reports = st.checkbox(
                "Report autopilot",
                value=bool(prefs.get("auto_reports", 1)),
                key="notif_auto_reports",
            )
        d1, d2, d3 = st.columns(3)
        with d1:
            daily_digest = st.checkbox(
                "Digest giornaliero",
                value=bool(prefs.get("daily_digest", 1)),
                key="notif_daily_digest",
            )
        with d2:
            quiet_start = st.text_input(
                "Silenzio da",
                value=str(prefs.get("quiet_hours_start") or ""),
                placeholder="22:00",
                key="notif_quiet_start",
            )
        with d3:
            quiet_end = st.text_input(
                "Silenzio fino a",
                value=str(prefs.get("quiet_hours_end") or ""),
                placeholder="08:00",
                key="notif_quiet_end",
            )
        if st.button("Salva notifiche", key="notif_save", use_container_width=True):
            update_notification_preferences(account_id, 0, {
                "telegram_enabled": int(telegram_enabled),
                "approval_alerts": int(approval_alerts),
                "auto_reports": int(auto_reports),
                "daily_digest": int(daily_digest),
                "quiet_hours_start": quiet_start.strip(),
                "quiet_hours_end": quiet_end.strip(),
            })
            st.toast("Preferenze notifiche aggiornate.", icon="✅")
            st.rerun()

    if props:
        today_key = date.today().isoformat()
        source_labels = {
            "manual": "Inserito manualmente",
            "manual_calendar": "Modifica manuale calendario",
            "manual_lock": "Prezzo bloccato manualmente",
            "demo": "Dato demo",
            "price_range_midpoint": "Stimato da min/max",
            "property_current_price": "Dato proprieta",
            "channel_manager": "Channel manager",
            "ota_api": "OTA/API",
        }
        with st.expander("Prezzi attuali", expanded=False):
            st.caption(
                "Questi sono i prezzi di partenza usati dal motore. Finche non colleghiamo OTA/API "
                "li puoi aggiornare manualmente da qui."
            )
            for p in props:
                current_price, current_source = get_current_price_for_date(p, today_key)
                c1, c2, c3 = st.columns([2, 1, 1])
                with c1:
                    st.markdown(f"**{p.get('name', 'Proprieta')}**")
                    st.caption(source_labels.get(current_source, current_source))
                with c2:
                    new_current = st.number_input(
                        "Prezzo attuale",
                        min_value=1.0,
                        max_value=10000.0,
                        value=float(current_price),
                        step=1.0,
                        key=f"current_price_{p['id']}_{today_key}",
                    )
                with c3:
                    st.markdown("<br>", unsafe_allow_html=True)
                    if st.button("Salva", key=f"save_current_price_{p['id']}", use_container_width=True):
                        upsert_calendar_price({
                            "account_id": account_id,
                            "property_id": int(p["id"]),
                            "date": today_key,
                            "current_price": float(new_current),
                            "current_price_source": "manual",
                            "recommended_price": None,
                            "status": "current",
                            "notes": "Prezzo corrente impostato dalla dashboard.",
                        })
                        st.toast("Prezzo corrente aggiornato.", icon="âœ…")
                        st.rerun()

    st.markdown("---")

    # ── Card per ogni proprietà ────────────────────────────────────────────────
    if props:
        mode_cfg = {
            "advisory": {"label": "💡 Manuale",  "color": "#6366f1", "bg": "#eef2ff",
                         "desc": "PricePilot suggerirà prezzi ottimali basati su domanda, mercato e occupazione."},
            "approval": {"label": "✅ Approvazione",  "color": "#f59e0b", "bg": "#fffbeb",
                         "desc": "Conferma su Telegram; sync OTA quando il channel manager sara collegato."},
            "auto":     {"label": "🤖 Automatico",   "color": "#10b981", "bg": "#f0fdf4",
                         "desc": "Autopilot completo quando sara collegata una sync reale."},
        }

        ncols = min(len(props), 3)
        card_cols = st.columns(ncols)
        for idx, p in enumerate(props):
            effective_plan = get_effective_plan_for_property(p)
            mode = effective_sync_mode(effective_plan, p.get("sync_mode", "advisory"))
            mcfg = mode_cfg.get(mode, mode_cfg["advisory"])
            plan_cfg = get_plan(effective_plan)
            try:
                from pricepilot.core.database import get_telegram_link_by_property
                tg = get_telegram_link_by_property(p["id"])
                tg_status = (f"🔔 @{tg['telegram_username'] or 'collegato'}"
                             if tg and tg.get("chat_id") else "🔕 non collegato")
            except Exception:
                tg_status = "—"

            _cn  = _html.escape(str(p.get('name', '')))
            _cc  = _html.escape(str(p.get('city', '') or '—'))
            _cpl = _html.escape(str(p.get('platform', '') or '').upper() or '—')

            with card_cols[idx % ncols]:
                st.markdown(
                    f'<div style="background:{mcfg["bg"]};border:1.5px solid {mcfg["color"]}33;'
                    f'border-radius:14px;padding:16px 18px;margin-bottom:12px">'
                    f'<div style="font-weight:800;font-size:1.05rem;margin-bottom:2px">{_cn}</div>'
                    f'<div style="font-size:0.82rem;color:#555;margin-bottom:1px">📍 {_cc}</div>'
                    f'<div style="font-size:0.78rem;color:#9ca3af;margin-bottom:6px">{_cpl}</div>'
                    f'<div style="margin:6px 0">'
                    f'<span style="background:#111827;color:white;'
                    f'padding:3px 10px;border-radius:20px;font-size:0.8rem;'
                    f'font-weight:600;margin-right:6px">{plan_cfg["label"]}</span>'
                    f'<span style="background:{mcfg["color"]};color:white;'
                    f'padding:3px 10px;border-radius:20px;font-size:0.8rem;'
                    f'font-weight:600">{mcfg["label"]}</span>'
                    f'</div>'
                    f'<div style="font-size:0.75rem;color:#9ca3af;margin-top:4px">{mcfg["desc"]}</div>'
                    f'<div style="font-size:0.78rem;color:#6b7280;margin-top:6px">'
                    f'💰 €{p["min_price"]:.0f} – €{p["max_price"]:.0f} &nbsp;·&nbsp; {tg_status}'
                    f'</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    else:
        st.markdown(
            '<div class="alert-blue">ℹ️ <b>Nessuna proprietà registrata.</b> '
            'Usa il form qui sotto per aggiungerne una.</div>',
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # ── Selettore: crea nuova o modifica esistente ────────────────────────────
    prop_ids     = ["➕ Nuova proprietà"] + [f"{p['id']} – {p['name']}" for p in props]
    selected_str = st.selectbox(
        "Modifica una proprietà esistente o creane una nuova",
        prop_ids,
        key="prop_form_sel",
    )

    existing = None
    if selected_str != "➕ Nuova proprietà":
        sel_id   = int(selected_str.split(" – ")[0])
        existing = next((p for p in props if p["id"] == sel_id), None)

    # ── Inizializza session state per il form a step ──────────────────────────
    form_key  = f"prop_step_{selected_str}"
    draft_key = f"prop_draft_{selected_str}"   # dizionario non legato a widget
    if form_key not in st.session_state:
        st.session_state[form_key] = 1

    # Quando si cambia selezione, resetta step E draft
    prev_sel_key = "prop_form_prev_sel"
    if st.session_state.get(prev_sel_key) != selected_str:
        st.session_state[form_key] = 1
        st.session_state.pop(draft_key, None)   # pulisce il draft del form precedente
        st.session_state[prev_sel_key] = selected_str

    current_step = st.session_state[form_key]

    # Carica/inizializza il draft dalla proprietà esistente (solo al primo ingresso)
    if draft_key not in st.session_state:
        st.session_state[draft_key] = {
            "name":     existing.get("name", "")         if existing else "",
            "city":     existing.get("city", "")         if existing else "",
            "platform": existing.get("platform", "airbnb") if existing else "airbnb",
            "url":      existing.get("listing_url", "")  if existing else "",
            "lid":      existing.get("listing_id", "")   if existing else "",
            "strategy": existing.get("strategy", "balanced") if existing else "balanced",
            "plan":     get_effective_plan_for_property(existing) if existing else "free",
        }
    is_edit = existing is not None
    form_title = f"✏️ Modifica: **{existing['name']}**" if is_edit else "➕ Nuova Proprietà"

    # Header del form a step
    step_names = ["Info Base", "Strategia Pricing", "Fasce di Prezzo"]
    steps_html = "".join([
        f'<span style="margin:0 6px;font-size:0.82rem;font-weight:'
        f'{"800" if i + 1 == current_step else "400"};color:'
        f'{"white" if i + 1 == current_step else "rgba(255,255,255,0.6)"}">'
        f'{"●" if i + 1 == current_step else "○"} {i + 1}. {name}</span>'
        for i, name in enumerate(step_names)
    ])
    st.markdown(
        f'<div class="step-header">'
        f'<div style="font-size:0.78rem;opacity:0.8;margin-bottom:2px">{form_title}</div>'
        f'<div style="font-size:1.05rem;font-weight:700">'
        f'Passo {current_step} di 3 – {step_names[current_step - 1]}</div>'
        f'<div style="margin-top:6px">{steps_html}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 1 – Informazioni base
    # ─────────────────────────────────────────────────────────────────────────
    if current_step == 1:
        _draft1 = st.session_state[draft_key]     # shortcut al draft corrente

        sc1, sc2 = st.columns(2)
        with sc1:
            pf_name = st.text_input(
                "Nome proprietà *",
                value=_draft1.get("name", ""),
                key="pf_name",
                placeholder="Es. Villa Roma Centro",
            )
            pf_city = st.text_input(
                "Città",
                value=_draft1.get("city", ""),
                key="pf_city",
                placeholder="Es. Roma",
            )
        with sc2:
            platform_opts = ["airbnb", "booking", "vrbo", "direct", "other"]
            _plat_default = _draft1.get("platform", "airbnb")
            _plat_idx     = platform_opts.index(_plat_default) \
                            if _plat_default in platform_opts else 0
            pf_platform = st.selectbox(
                "Piattaforma",
                platform_opts,
                index=_plat_idx,
                key="pf_platform",
                format_func=lambda x: {"airbnb": "🏠 Airbnb", "booking": "🌐 Booking.com",
                                        "vrbo": "🏡 Vrbo", "direct": "📋 Diretto",
                                        "other": "🔗 Altro"}.get(x, x),
            )
            pf_url = st.text_input(
                "Listing URL (opzionale)",
                value=_draft1.get("url", ""),
                key="pf_url",
                placeholder="https://airbnb.com/rooms/...",
            )
            pf_lid = st.text_input(
                "Listing ID (opzionale)",
                value=_draft1.get("lid", ""),
                key="pf_lid",
                help="Necessario per la modalità Auto Apply",
                placeholder="Es. 12345678",
            )

        # ── OTA name auto-detection (item 4) ─────────────────────────────────
        _suggested = _suggest_name_from_url(pf_url, pf_platform)
        _cur_name  = st.session_state.get("pf_name", pf_name).strip()
        if _suggested and _suggested.lower() != _cur_name.lower():
            st.markdown(
                f'<div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;'
                f'padding:8px 12px;margin:4px 0;font-size:0.85rem;color:#1e40af">'
                f'💡 <b>Nome rilevato dall\'URL:</b> {_suggested}</div>',
                unsafe_allow_html=True,
            )
            if st.button(
                f"✅ Usa «{_suggested}»", key="use_ota_name",
                help="Sostituisce il campo nome con il valore estratto dall'URL del listing",
            ):
                st.session_state["pf_name"] = _suggested
                st.session_state[draft_key]["name"] = _suggested
                st.rerun()

        # ── Multi-OTA: connessioni aggiuntive (item 5) ───────────────────────
        _ota_extras_key = f"ota_extras_{selected_str}"
        st.session_state.setdefault(_ota_extras_key, [])

        # Per modalità edit: carica integrazioni esistenti dalla DB (una sola volta)
        _ota_loaded_key = f"ota_loaded_{selected_str}"
        if existing and not st.session_state.get(_ota_loaded_key):
            try:
                _db_integ = get_property_integrations(existing["id"])
                # Mostra solo le secondarie (non la piattaforma principale)
                _extra_from_db = [
                    {"id": r["id"], "platform": r["platform"],
                     "url": r["listing_url"], "lid": r["listing_id"],
                     "_from_db": True}
                    for r in _db_integ
                    if r["platform"] != existing.get("platform", "airbnb")
                ]
                if _extra_from_db:
                    st.session_state[_ota_extras_key] = _extra_from_db
            except Exception:
                pass
            st.session_state[_ota_loaded_key] = True

        _ota_extras = st.session_state[_ota_extras_key]

        with st.expander(
            f"🔗 Connessioni OTA aggiuntive ({len(_ota_extras)} aggiunte)" if _ota_extras
            else "🔗 Aggiungi connessioni OTA aggiuntive (opzionale)"
        ):
            st.caption("Gestisci listing su più piattaforme per la stessa proprietà.")
            _plat_labels = {"airbnb": "🏠 Airbnb", "booking": "🌐 Booking.com",
                            "vrbo": "🏡 Vrbo", "direct": "📋 Diretto", "other": "🔗 Altro"}

            _to_remove = []
            for _oi, _ota in enumerate(_ota_extras):
                _ota_col1, _ota_col2, _ota_col3, _ota_col4 = st.columns([2, 3, 2, 1])
                with _ota_col1:
                    _ota_plat_opts = ["airbnb", "booking", "vrbo", "direct", "other"]
                    _ota_plat_idx  = _ota_plat_opts.index(_ota.get("platform", "airbnb")) \
                                     if _ota.get("platform", "airbnb") in _ota_plat_opts else 0
                    _new_plat = st.selectbox(
                        "Piattaforma", _ota_plat_opts, index=_ota_plat_idx,
                        format_func=lambda x: _plat_labels.get(x, x),
                        key=f"ota_plat_{selected_str}_{_oi}",
                        label_visibility="collapsed",
                    )
                    st.session_state[_ota_extras_key][_oi]["platform"] = _new_plat
                with _ota_col2:
                    _new_url = st.text_input(
                        "URL", value=_ota.get("url", ""),
                        key=f"ota_url_{selected_str}_{_oi}",
                        placeholder="https://booking.com/...",
                        label_visibility="collapsed",
                    )
                    st.session_state[_ota_extras_key][_oi]["url"] = _new_url
                with _ota_col3:
                    _new_lid = st.text_input(
                        "Listing ID", value=_ota.get("lid", ""),
                        key=f"ota_lid_{selected_str}_{_oi}",
                        placeholder="ID",
                        label_visibility="collapsed",
                    )
                    st.session_state[_ota_extras_key][_oi]["lid"] = _new_lid
                with _ota_col4:
                    if st.button("🗑️", key=f"ota_del_{selected_str}_{_oi}",
                                 help="Rimuovi questa connessione"):
                        _to_remove.append(_oi)

            if _to_remove:
                for _idx in reversed(_to_remove):
                    _rem = st.session_state[_ota_extras_key].pop(_idx)
                    # Marca per eliminazione da DB se era già persistita
                    if _rem.get("_from_db") and _rem.get("id"):
                        _del_key = f"ota_del_ids_{selected_str}"
                        st.session_state.setdefault(_del_key, set()).add(_rem["id"])
                st.rerun()

            if st.button("➕ Aggiungi connessione OTA", key=f"ota_add_{selected_str}"):
                st.session_state[_ota_extras_key].append(
                    {"platform": "booking", "url": "", "lid": "", "_from_db": False}
                )
                st.rerun()

        col_next = st.columns([3, 1])[1]
        with col_next:
            if st.button("Avanti →", key="step1_next", use_container_width=True, type="primary"):
                # Leggi i valori correnti dai widget prima che st.rerun() pulisca i loro key
                _name_now = st.session_state.get("pf_name", pf_name).strip()
                if not _name_now:
                    st.error("❌ Il nome della proprietà è obbligatorio.")
                else:
                    # Salva nel draft (persistente tra step) prima del rerun
                    st.session_state[draft_key].update({
                        "name":     st.session_state.get("pf_name", pf_name),
                        "city":     st.session_state.get("pf_city", pf_city),
                        "platform": st.session_state.get("pf_platform", pf_platform),
                        "url":      st.session_state.get("pf_url", pf_url),
                        "lid":      st.session_state.get("pf_lid", pf_lid),
                    })
                    st.session_state[form_key] = 2
                    st.rerun()

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 2 – Strategia Pricing
    # ─────────────────────────────────────────────────────────────────────────
    elif current_step == 2:
        st.markdown("##### Scegli la strategia di pricing più adatta alla tua struttura:")

        strategy_info = {
            "conservative": {
                "label": "🛡️ Conservativa",
                "desc":  "Prezzi stabili e alta occupazione. Ideale per chi vuole ridurre il rischio di periodi vuoti.",
                "color": "#3b82f6",
            },
            "balanced": {
                "label": "⚖️ Bilanciata",
                "desc":  "Ottimizza revenue e occupazione. La scelta migliore per la maggior parte degli host.",
                "color": "#8b5cf6",
            },
            "aggressive": {
                "label": "🚀 Aggressiva",
                "desc":  "Massimizza il prezzo quando la domanda è alta. Alta variazione, alto potenziale.",
                "color": "#f59e0b",
            },
            "premium": {
                "label": "💎 Premium",
                "desc":  "Per immobili di fascia alta. Prezzo elevato anche fuori stagione.",
                "color": "#10b981",
            },
        }

        # Usa il valore del draft (persistente) come default per il radio
        _strat_default = st.session_state[draft_key].get("strategy", "balanced")
        _strat_keys    = list(strategy_info.keys())
        _strat_idx     = _strat_keys.index(_strat_default) \
                         if _strat_default in _strat_keys else 1

        pf_strategy = st.radio(
            "Strategia",
            options=_strat_keys,
            format_func=lambda k: strategy_info[k]["label"],
            index=_strat_idx,
            key="pf_strategy",
            horizontal=False,
        )

        # Descrizione dettagliata strategia selezionata
        info = strategy_info[pf_strategy]
        st.markdown(
            f'<div style="background:{info["color"]}18;border-left:4px solid {info["color"]};'
            f'border-radius:8px;padding:12px 16px;margin:8px 0">'
            f'<b>{info["label"]}</b><br>'
            f'<span style="font-size:0.88rem;color:#555">{info["desc"]}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

        col_back, col_next = st.columns(2)
        with col_back:
            if st.button("← Indietro", key="step2_back", use_container_width=True):
                st.session_state[form_key] = 1
                st.rerun()
        with col_next:
            if st.button("Avanti →", key="step2_next", use_container_width=True, type="primary"):
                # Salva la strategia nel draft prima che il widget venga rimosso
                st.session_state[draft_key]["strategy"] = st.session_state.get(
                    "pf_strategy", pf_strategy
                )
                st.session_state[form_key] = 3
                st.rerun()

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 3 – Fasce di Prezzo + Modalità Pricing
    # ─────────────────────────────────────────────────────────────────────────
    elif current_step == 3:
        st.markdown("##### Imposta le fasce di prezzo e la modalità di gestione:")

        _draft3 = st.session_state[draft_key]   # draft con name/city/platform/strategy

        # Usa valori dal draft (persistente) come default dei number_input
        _min_default = float(
            _draft3.get("min_price",
                        existing["min_price"] if existing else 50.0)
        )
        _max_default = float(
            _draft3.get("max_price",
                        existing["max_price"] if existing else 300.0)
        )

        pc1, pc2 = st.columns(2)
        with pc1:
            pf_min = st.number_input(
                "💰 Prezzo minimo (€)",
                min_value=10.0, max_value=1000.0,
                value=_min_default,
                step=5.0,
                key="pf_min",
                help="PricePilot non scenderà mai sotto questo prezzo.",
            )
        with pc2:
            pf_max = st.number_input(
                "💰 Prezzo massimo (€)",
                min_value=50.0, max_value=5000.0,
                value=_max_default,
                step=10.0,
                key="pf_max",
                help="PricePilot non supererà mai questo prezzo.",
            )

        st.markdown("---")
        st.markdown("##### Piano")

        account = get_account(account_id) or {"plan": "free"}
        pf_plan = str(account.get("plan") or "free").lower()
        _render_readonly_plan_box(pf_plan, compact=True)
        st.button("Cambia piano", key="pf_change_plan_disabled", use_container_width=True, disabled=True)

        mode_opts = {
            "advisory": {
                "label": "💡 Manuale",
                "desc":  "PricePilot suggerisce prezzi ma non li applica. Decidi tu ogni volta.",
                "color": "#6366f1",
            },
            "approval": {
                "label": "✅ Approvazione Telegram",
                "desc":  "PricePilot invia il suggerimento su Telegram. La sync OTA si attiva quando colleghiamo un channel manager.",
                "color": "#f59e0b",
            },
            "auto": {
                "label": "🤖 Automatico",
                "desc":  "Autopilot completo previsto per il piano Pro con channel manager reale collegato.",
                "color": "#10b981",
            },
        }

        pf_mode = effective_sync_mode(pf_plan, existing.get("sync_mode", "advisory") if existing else "advisory")

        minfo = mode_opts[pf_mode]
        st.markdown(
            f'<div style="background:{minfo["color"]}12;border-left:4px solid {minfo["color"]};'
            f'border-radius:8px;padding:10px 14px;margin:8px 0 16px">'
            f'<b>{minfo["label"]}</b><br>'
            f'<span style="font-size:0.87rem;color:#555">{minfo["desc"]}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

        if pf_min >= pf_max:
            st.error("❌ Il prezzo minimo deve essere inferiore al prezzo massimo.")

        col_back, col_save, col_del = st.columns([1, 2, 1])
        with col_back:
            if st.button("← Indietro", key="step3_back", use_container_width=True):
                st.session_state[form_key] = 2
                st.rerun()
        with col_save:
            if st.button(
                "💾 Salva proprietà" if is_edit else "➕ Crea proprietà",
                key="step3_save",
                use_container_width=True,
                type="primary",
                disabled=(pf_min >= pf_max),
            ):
                # Costruisci i dati dal draft (step 1+2, persistente) +
                # dai widget correnti (step 3, ancora visibili)
                _draft_final = st.session_state.get(draft_key, {})
                data = {
                    "account_id":   account_id,
                    "name":        _draft_final.get("name", "").strip(),
                    "city":        _draft_final.get("city", ""),
                    "platform":    _draft_final.get("platform", "airbnb"),
                    "listing_url": _draft_final.get("url", ""),
                    "listing_id":  _draft_final.get("lid", ""),
                    "min_price":   float(pf_min),
                    "max_price":   float(pf_max),
                    "plan":        str(pf_plan),
                    "sync_mode":   str(pf_mode),
                    "strategy":    _draft_final.get("strategy", "balanced"),
                }

                # Validazione anticipata (non delegare solo a _validate)
                if not data["name"]:
                    st.error("❌ Il nome della proprietà è obbligatorio. "
                             "Torna al Passo 1 e inseriscilo.")
                else:
                    try:
                        if is_edit:
                            update_property(existing["id"], data)
                            prop_id_saved = existing["id"]
                            st.success(f"✅ **{data['name']}** aggiornata con successo!")
                        else:
                            new_prop = create_property(data)
                            prop_id_saved = new_prop["id"]
                            st.session_state["active_prop_id"] = new_prop["id"]
                            st.success(
                                f"✅ Proprietà **{data['name']}** creata con successo!"
                            )

                        # ── Salva / elimina integrazioni OTA extra ────────────────
                        st.session_state["active_prop_id"] = prop_id_saved
                        _remember_saved_price_limits(
                            prop_id_saved,
                            data["min_price"],
                            data["max_price"],
                        )
                        queue_property_pricing_widget_reset(
                            prop_id_saved,
                            sidebar=True,
                            pricing_tab=True,
                        )
                        st.cache_data.clear()

                        _ota_extras_key = f"ota_extras_{selected_str}"
                        _del_key        = f"ota_del_ids_{selected_str}"
                        # Elimina quelle marcate per cancellazione
                        for _del_id in st.session_state.get(_del_key, set()):
                            try:
                                delete_property_integration(_del_id)
                            except Exception:
                                pass
                        # Salva / aggiorna quelle nuove o modificate
                        for _ota in st.session_state.get(_ota_extras_key, []):
                            if _ota.get("platform") and _ota.get("platform") != data["platform"]:
                                try:
                                    upsert_property_integration({
                                        "property_id": prop_id_saved,
                                        "platform":    _ota["platform"],
                                        "listing_url": _ota.get("url", ""),
                                        "listing_id":  _ota.get("lid", ""),
                                        "is_primary":  0,
                                    })
                                except Exception:
                                    pass

                        # Pulisci draft e torna allo step 1
                        st.session_state.pop(draft_key, None)
                        st.session_state.pop(_ota_extras_key, None)
                        st.session_state.pop(_del_key, None)
                        st.session_state.pop(f"ota_loaded_{selected_str}", None)
                        st.session_state[form_key] = 1
                        st.rerun()
                    except ValueError as e:
                        st.error(f"❌ {e}")
        with col_del:
            if is_edit and st.button(
                "🗑️ Elimina", key="step3_del",
                use_container_width=True, type="secondary",
            ):
                from pricepilot.core.database import delete_property
                delete_property(existing["id"])
                st.warning(f"🗑️ Proprietà **{existing['name']}** eliminata.")
                st.session_state.pop(draft_key, None)
                st.session_state[form_key] = 1
                st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# TAB: PRICING SIMULATOR
# ═════════════════════════════════════════════════════════════════════════════

def tab_simulator():
    st.markdown('<div class="section-title">🔬 Simulatore di Prezzi</div>',
                unsafe_allow_html=True)
    st.markdown("Simula scenari di pricing senza salvare nel database.")

    props = list_properties(account_id=current_account_id())
    if not props:
        st.warning("Crea prima una proprietà nella tab **🏠 Proprietà**.")
        return

    # ── Controlli simulazione ─────────────────────────────────────────────────
    col_ctrl, col_res = st.columns([1, 1], gap="large")

    with col_ctrl:
        st.markdown("#### ⚙️ Parametri")

        prop_id = st.selectbox(
            "Proprietà",
            options=[p["id"] for p in props],
            format_func=lambda x: next((p["name"] for p in props if p["id"] == x), str(x)),
        )
        prop = next((p for p in props if p["id"] == prop_id), props[0])

        sim_date = st.date_input("📅 Data simulazione",
                                 value=date.today() + timedelta(days=7))

        occupancy = st.slider("📊 Occupazione (%)", 0, 100, 65) / 100

        competitor_count = st.slider("🏘️ Competitor da analizzare", 2, 12, 8)

        season_factor = st.slider(
            "🌡️ Stagionalità manuale", 0.5, 2.0, 1.0, step=0.05,
            help="1.0 = normale | 1.3 = alta stagione | 0.7 = bassa stagione"
        )

        event_options = ["none", "holiday", "conference", "festival",
                         "concert", "marathon", "local_fair", "fair"]
        event_sel = st.selectbox("🎉 Evento", event_options)

        event_factor = 1.0
        if event_sel != "none":
            event_factor = st.slider("🎯 Intensità evento", 1.0, 2.0, 1.2, step=0.05)

        sim_btn = st.button("▶️ Esegui Simulazione", use_container_width=True, type="primary")

    if sim_btn:
        # ── Analisi mercato ───────────────────────────────────────────────────
        has_event = event_sel != "none"
        competitors = simulate_competitors(
            base_price  = prop["base_price"] if hasattr(prop, "base_price") else
                          (prop["min_price"] + prop["max_price"]) / 2,
            target_date = sim_date,
            event       = event_sel,
            n           = competitor_count,
        )
        stats = calculate_market_stats(competitors)

        # ── Prezzo raccomandato ───────────────────────────────────────────────
        result = calculate_recommended_price(
            base_price       = (prop["min_price"] + prop["max_price"]) / 2,
            market_avg       = stats["market_avg"],
            occupancy        = occupancy,
            target_date      = sim_date,
            has_event        = has_event,
            min_price        = float(prop["min_price"]),
            max_price        = float(prop["max_price"]),
            competitor_count = competitor_count,
            season_factor    = season_factor,
            event_factor     = event_factor,
        )

        rec   = result["recommended_price"]
        conf  = result["confidence_score"]
        delta_mkt  = result["delta_vs_market"]
        delta_base = result["delta_vs_base"]

        with col_res:
            # ── Recommended price card ────────────────────────────────────────
            conf_color = "#2ecc71" if conf >= 0.7 else "#f39c12" if conf >= 0.5 else "#e74c3c"
            sign_m = "+" if delta_mkt >= 0 else ""
            sign_b = "+" if delta_base >= 0 else ""

            st.markdown(f"""
            <div class="price-card">
                <div class="price-label">💡 Prezzo Raccomandato</div>
                <div class="price-big">€ {rec:.2f}</div>
                <div style="font-size:0.95rem; margin-top:8px">
                    vs Mercato: <b>{sign_m}{delta_mkt:.1f}%</b>
                    &nbsp;|&nbsp;
                    vs Base: <b>{sign_b}{delta_base:.1f}%</b>
                </div>
                <div style="margin-top:10px; font-size:0.9rem; opacity:0.9">
                    Confidenza:
                    <span style="color:{conf_color}; font-weight:700">{conf*100:.0f}%</span>
                    &nbsp;|&nbsp;
                    {'🏖️ Weekend' if result['is_weekend'] else '📅 Feriale'}
                    {'&nbsp;|&nbsp; 🎉 ' + event_sel if has_event else ''}
                </div>
            </div>
            """, unsafe_allow_html=True)

        # ── Market Overview ───────────────────────────────────────────────────
        st.markdown('<div class="section-title">📊 Panoramica Mercato</div>',
                    unsafe_allow_html=True)

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("📊 Media Mercato",   f"€{stats['market_avg']:.2f}")
        m2.metric("📉 Min Mercato",     f"€{stats['market_min']:.2f}")
        m3.metric("📈 Max Mercato",     f"€{stats['market_max']:.2f}")
        m4.metric("📏 Dev. Std.",        f"€{stats['market_std']:.2f}")
        m5.metric("🏘️ Competitor",     stats["competitor_count"])

        # ── Breakdown fattori ─────────────────────────────────────────────────
        st.markdown('<div class="section-title">🔍 Breakdown Fattori</div>',
                    unsafe_allow_html=True)
        breakdown = result["breakdown"]
        bd_rows = [{"Fattore": k, "Effetto": v} for k, v in breakdown.items()]
        st.dataframe(pd.DataFrame(bd_rows), use_container_width=True, hide_index=True)

        # ── Grafico competitor ────────────────────────────────────────────────
        st.markdown('<div class="section-title">🏘️ Prezzi Competitor</div>',
                    unsafe_allow_html=True)

        df_comp = pd.DataFrame(competitors)
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=df_comp["name"], y=df_comp["price"],
            marker_color=[
                "#667eea" if abs(c["price"] - rec) < 3
                else ("#2ecc71" if c["price"] < rec else "#e74c3c")
                for c in competitors
            ],
            text=df_comp["price"].apply(lambda x: f"€{x:.0f}"),
            textposition="outside",
        ))
        fig.add_hline(y=rec, line_dash="dash", line_color="#f39c12",
                      annotation_text=f"Raccomandato: €{rec:.2f}")
        fig.add_hline(y=stats["market_avg"], line_dash="dot", line_color="#9b59b6",
                      annotation_text=f"Media: €{stats['market_avg']:.2f}")
        fig.update_layout(
            title=f"Competitor – {sim_date.strftime('%d %b %Y')}",
            xaxis_title="Proprietà", yaxis_title="Prezzo (€)",
            height=380, showlegend=False,
            plot_bgcolor="white", paper_bgcolor="white",
            xaxis_tickangle=-30,
        )
        st.plotly_chart(fig, use_container_width=True, config=PLOTLY_CONFIG)

        # ── Scatter: price vs occupancy ───────────────────────────────────────
        df_comp["size"] = df_comp["rating"] * 3
        fig2 = px.scatter(
            df_comp, x="occupancy", y="price",
            size="size", color="beds",
            hover_name="name",
            labels={"occupancy": "Occupazione stimata", "price": "Prezzo (€)", "beds": "Camere"},
            title="Competitor: Occupazione vs Prezzo",
            color_continuous_scale="Viridis",
        )
        fig2.add_hline(y=rec, line_dash="dash", line_color="#f39c12",
                       annotation_text=f"Nostro: €{rec:.2f}")
        fig2.update_layout(height=360, plot_bgcolor="white", paper_bgcolor="white")
        st.plotly_chart(fig2, use_container_width=True, config=PLOTLY_CONFIG)

        # ── Market history per proprietà ──────────────────────────────────────
        mkt_hist = get_market_history(property_id=prop_id, limit=30, account_id=current_account_id())
        if mkt_hist:
            st.markdown('<div class="section-title">📈 Storico Mercato (ultimi 30 giorni)</div>',
                        unsafe_allow_html=True)
            df_mh = pd.DataFrame(mkt_hist)[["date", "market_avg", "market_min", "market_max"]].copy()
            df_mh["date"] = pd.to_datetime(df_mh["date"])
            df_mh = df_mh.sort_values("date")
            fig3 = go.Figure()
            fig3.add_trace(go.Scatter(
                x=df_mh["date"], y=df_mh["market_max"],
                name="Max", line=dict(color="#e8e8e8", width=0), fill=None, showlegend=False,
            ))
            fig3.add_trace(go.Scatter(
                x=df_mh["date"], y=df_mh["market_min"],
                name="Range Mercato", line=dict(color="#e8e8e8", width=0),
                fill="tonexty", fillcolor="rgba(102,126,234,0.15)", showlegend=True,
            ))
            fig3.add_trace(go.Scatter(
                x=df_mh["date"], y=df_mh["market_avg"],
                name="Media Mercato", line=dict(color="#9b59b6", width=2),
            ))
            fig3.update_layout(
                title="Storico media mercato",
                height=320, plot_bgcolor="white", paper_bgcolor="white",
                hovermode="x unified",
            )
            st.plotly_chart(fig3, use_container_width=True, config=PLOTLY_CONFIG)


# ═════════════════════════════════════════════════════════════════════════════
# CALENDAR – helpers
# ═════════════════════════════════════════════════════════════════════════════

def _generate_calendar_prices(prop: dict, cfg: dict, days: int = 90) -> list:
    """
    Genera dati di pricing per i prossimi `days` giorni usando il motore completo.
    Ogni giorno passa per: demand_factor, market_price, guardrails, min/max, strategia.
    """
    import hashlib
    today  = date.today()
    bp     = (float(prop["min_price"]) + float(prop["max_price"])) / 2
    result = []

    for i in range(days):
        d = today + timedelta(days=i)

        # Occupancy deterministica (hash property+data per consistenza)
        h   = int(hashlib.md5((str(prop["id"]) + d.isoformat()).encode()).hexdigest(), 16)
        occ = round(0.45 + (h % 1000) / 1000 * 0.45, 2)

        # Evento
        evt      = get_event_for_date(d)
        has_evt  = evt is not None
        evt_str  = event_to_string(evt) if evt else "none"

        # Competitor simulati
        comps = simulate_competitors(bp, d, evt_str, 8)
        stats = calculate_market_stats(comps)

        # Prezzo raccomandato – tutto il pipeline pricing v2
        rec = calculate_recommended_price(
            base_price       = bp,
            market_avg       = stats["market_avg"],
            occupancy        = occ,
            target_date      = d,
            has_event        = has_evt,
            min_price        = float(prop["min_price"]),
            max_price        = float(prop["max_price"]),
            competitor_count = len(comps),
        )

        result.append({
            "date":              d,
            "date_iso":          d.isoformat(),
            "recommended_price": rec["recommended_price"],
            "market_avg":        stats["market_avg"],
            "is_weekend":        rec["is_weekend"],
            "has_event":         has_evt,
            "event_name":        evt["name"] if evt else "",
            "occupancy":         occ,
            "delta_vs_market":   rec["delta_vs_market"],
            "confidence_score":  rec["confidence_score"],
            "breakdown":         rec["breakdown"],
            "safety_note":       rec["safety_note"],
            "min_price":         float(prop["min_price"]),
            "max_price":         float(prop["max_price"]),
        })
    return result


def _get_calendar_prices(prop: dict, cfg: dict) -> list:
    """Ritorna i prezzi calendario dalla cache session_state, o li rigenera."""
    pid        = prop["id"]
    cache_key  = f"cal_prices_{pid}"
    hash_key   = f"cal_prop_hash_{pid}"
    prop_hash  = hash((float(prop["min_price"]), float(prop["max_price"]), pid))

    if (cache_key not in st.session_state
            or st.session_state.get(hash_key) != prop_hash):
        with st.spinner("Generazione prezzi calendario (90 giorni)…"):
            prices = _generate_calendar_prices(prop, cfg)
        st.session_state[cache_key] = prices
        st.session_state[hash_key]  = prop_hash

    return st.session_state[cache_key]


# ═════════════════════════════════════════════════════════════════════════════
# TAB: SMART PRICING CALENDAR
# ═════════════════════════════════════════════════════════════════════════════

def tab_calendar(cfg: dict):
    """
    Smart Pricing Calendar – prezzi ottimali per i prossimi 90 giorni.
    Ogni prezzo passa per il motore pricing v2 completo (demand, guardrail, strategia).
    """
    import calendar as _cal

    # ── Header ────────────────────────────────────────────────────────────────
    st.markdown(
        '<div style="font-size:1.5rem;font-weight:800;color:#1a1a2e;margin-bottom:2px">'
        '📅 Smart Pricing Calendar</div>'
        '<p style="color:#666;font-size:0.93rem;margin-top:0">Prezzi ottimali calcolati '
        'con il motore pricing completo: domanda, eventi, guardrail e strategia.</p>',
        unsafe_allow_html=True,
    )

    # ── Selezione proprietà ───────────────────────────────────────────────────
    props = list_properties(account_id=current_account_id())
    if not props:
        st.warning("Crea prima una proprietà nella tab **🏡 Proprietà**.")
        return

    today          = date.today()
    active_pid     = st.session_state.get("active_prop_id", props[0]["id"])

    cc1, cc2, cc3 = st.columns([2, 2, 1])
    with cc1:
        sel_pid = st.selectbox(
            "🏠 Proprietà",
            options=[p["id"] for p in props],
            format_func=lambda x: next((p["name"] for p in props if p["id"] == x), str(x)),
            index=next((i for i, p in enumerate(props) if p["id"] == active_pid), 0),
            key="cal_prop_sel",
        )
    prop = next((p for p in props if p["id"] == sel_pid), props[0])

    # ── Selezione mese ─────────────────────────────────────────────────────────
    month_opts = [
        today.replace(day=1),
        (today.replace(day=1) + timedelta(days=32)).replace(day=1),
        (today.replace(day=1) + timedelta(days=65)).replace(day=1),
    ]
    month_labels = [m.strftime("%B %Y") for m in month_opts]

    with cc2:
        sel_month_lbl = st.selectbox("📆 Mese", options=month_labels, key="cal_month_sel")

    sel_month_date = month_opts[month_labels.index(sel_month_lbl)]
    cal_year  = sel_month_date.year
    cal_month = sel_month_date.month

    with cc3:
        if st.button("🔄 Rigenera", key="cal_regen", use_container_width=True,
                     help="Ricalcola i prezzi per i prossimi 90 giorni"):
            ck = f"cal_prices_{sel_pid}"
            if ck in st.session_state:
                del st.session_state[ck]
            st.toast("Rigenero i prezzi…", icon="🔄")
            st.rerun()

    # ── Genera/preleva dati calendario (cached) ────────────────────────────────
    prices_list = _get_calendar_prices(prop, cfg)
    prices_dict = {p["date_iso"]: p for p in prices_list}
    stored_calendar = get_price_calendar(
        account_id=current_account_id(),
        property_id=sel_pid,
        date_from=today.isoformat(),
        date_to=(today + timedelta(days=89)).isoformat(),
        limit=120,
    )
    persisted_overrides = {}
    persisted_locks = {}
    for row in stored_calendar:
        status = str(row.get("status") or "").lower()
        row_date = str(row.get("date") or "")
        if not row_date or row.get("current_price") is None:
            continue
        if status == "locked":
            persisted_locks[row_date] = float(row["current_price"])
        elif status == "manual_override":
            persisted_overrides[row_date] = float(row["current_price"])

    session_overrides = st.session_state.setdefault(f"cal_overrides_{sel_pid}", {})
    session_locks = st.session_state.setdefault(f"cal_locks_{sel_pid}", {})
    overrides = {**persisted_overrides, **session_overrides}
    locks = {**persisted_locks, **session_locks}

    # ── Griglia calendario ────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown(
        f'<div class="section-title">📅 {sel_month_lbl}</div>',
        unsafe_allow_html=True,
    )

    first_wd, days_in_month = _cal.monthrange(cal_year, cal_month)

    # Lettura giorno selezionato da session_state
    sel_day = st.session_state.get("cal_selected_day")

    # ── Header giorni della settimana ─────────────────────────────────────────
    hdr_cols = st.columns(7)
    for _i, _n in enumerate(["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]):
        hdr_cols[_i].markdown(
            f'<div style="text-align:center;font-size:0.78rem;font-weight:700;'
            f'color:{"#e74c3c" if _i >= 5 else "#555"};padding:4px 0 6px">{_n}</div>',
            unsafe_allow_html=True,
        )

    # ── Celle del mese in righe settimanali ──────────────────────────────────
    # Costruiamo una lista piatta: None per i giorni prima dell'inizio
    day_cells = [None] * first_wd + list(range(1, days_in_month + 1))
    # Pad fino al multiplo di 7
    while len(day_cells) % 7:
        day_cells.append(None)

    for week_start in range(0, len(day_cells), 7):
        week = day_cells[week_start: week_start + 7]
        week_cols = st.columns(7)

        for col_idx, day_num in enumerate(week):
            with week_cols[col_idx]:
                if day_num is None:
                    st.markdown(
                        '<div style="min-height:54px"></div>',
                        unsafe_allow_html=True,
                    )
                    continue

                d       = date(cal_year, cal_month, day_num)
                d_iso   = d.isoformat()
                ddata   = prices_dict.get(d_iso, {})
                in_range = bool(ddata)

                is_locked    = d_iso in locks
                is_past      = d < today
                is_today_day = d == today
                is_ov        = d_iso in overrides
                is_sel       = sel_day is not None and d == sel_day

                rec_p  = ddata.get("recommended_price", 0)
                ov_p   = overrides.get(d_iso)
                lock_p = locks.get(d_iso)
                # Priorità: bloccato > override > raccomandato
                price  = lock_p if is_locked else (ov_p if ov_p else rec_p)
                market = ddata.get("market_avg", 0)
                wknd   = ddata.get("is_weekend", False)
                has_evt = ddata.get("has_event", False)

                # Colore barra indicatore sopra il pulsante
                if is_locked:
                    ind_color = "#c084fc"   # viola = bloccato
                elif is_today_day:
                    ind_color = "#764ba2"   # viola scuro = oggi
                elif has_evt:
                    ind_color = "#f59e0b"   # giallo = evento
                elif wknd:
                    ind_color = "#f97316"   # arancione = weekend
                elif in_range and market > 0 and price > market * 1.05:
                    ind_color = "#f87171"   # rosso = sopra mercato
                elif in_range and market > 0 and price < market * 0.95:
                    ind_color = "#4ade80"   # verde = sotto mercato
                else:
                    ind_color = "#d1d5db"   # grigio = normale

                # Emoji markers nella label del pulsante
                markers = ""
                if is_locked:
                    markers = "🔒"
                elif has_evt:
                    markers = "🎉"
                elif is_today_day:
                    markers = "⭐"
                elif wknd:
                    markers = "🏖️"
                if is_ov and not is_locked:
                    markers += "✏️"

                price_str = f"€{price:.0f}" if price > 0 else "—"
                btn_label = f"{day_num} {markers}\n{price_str}".strip()

                # Barra colorata indicatore (sopra il pulsante)
                st.markdown(
                    f'<div style="height:4px;border-radius:3px 3px 0 0;'
                    f'background:{ind_color};margin-bottom:1px"></div>',
                    unsafe_allow_html=True,
                )

                btn_type = "primary" if is_sel else "secondary"
                if st.button(
                    btn_label,
                    key=f"cal_btn_{d_iso}",
                    use_container_width=True,
                    type=btn_type,
                    disabled=is_past and not in_range,
                ):
                    st.session_state["cal_selected_day"] = d
                    # Sincronizza anche il date_input widget
                    st.session_state["cal_day_input"]    = d
                    st.rerun()

    # ── Legenda ────────────────────────────────────────────────────────────────
    st.markdown(
        '<div style="display:flex;gap:8px;flex-wrap:wrap;font-size:0.76rem;margin:4px 0 14px">'
        '<span style="background:#fff8f0;border-radius:4px;padding:2px 8px">🏖️ Weekend</span>'
        '<span style="background:#fff3cd;border-radius:4px;padding:2px 8px">🎉 Evento</span>'
        '<span style="background:#fde8e8;border-radius:4px;padding:2px 8px">'
        '&#128200; Sopra mercato</span>'
        '<span style="background:#e8f5e9;border-radius:4px;padding:2px 8px">'
        '&#128201; Sotto mercato</span>'
        '<span style="background:linear-gradient(135deg,#e8e0f8,#d4c5f5);'
        'border-radius:4px;padding:2px 8px">⭐ Oggi</span>'
        '<span style="background:linear-gradient(135deg,#667eea,#764ba2);color:white;'
        'border-radius:4px;padding:2px 8px">Selezionato</span>'
        '<span style="background:#f3e8ff;border-radius:4px;padding:2px 8px">'
        '🔒 Bloccato</span>'
        '<span>&#9999;&#65039; Modifica manuale</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    st.markdown("---")

    # ── Dettaglio giorno selezionato ──────────────────────────────────────────
    st.markdown(
        '<div class="section-title">🔍 Dettaglio Giorno</div>',
        unsafe_allow_html=True,
    )

    dc1, dc2 = st.columns([1, 1], gap="large")

    with dc1:
        min_cal = today
        max_cal = today + timedelta(days=89)

        # Inizializza cal_day_input in session_state solo se assente,
        # evitando il warning "created with default value but also set via Session State API"
        if "cal_day_input" not in st.session_state:
            st.session_state["cal_day_input"] = today

        # Assicura che il valore sia nel range valido
        _cur = st.session_state["cal_day_input"]
        if not isinstance(_cur, date):
            _cur = today
        st.session_state["cal_day_input"] = min(max(_cur, min_cal), max_cal)

        sel_day = st.date_input(
            "📅 Seleziona giorno",
            min_value=min_cal,
            max_value=max_cal,
            key="cal_day_input",
        )
        st.session_state["cal_selected_day"] = sel_day

    d_iso  = sel_day.isoformat()
    ddata  = prices_dict.get(d_iso, {})

    if not ddata:
        st.info("Giorno fuori dall'intervallo di 90 giorni.")
    else:
        rec_p    = ddata.get("recommended_price", 0)
        ov_p     = overrides.get(d_iso)
        lock_p   = locks.get(d_iso)
        is_locked_day = d_iso in locks
        # Priorità: bloccato > override > raccomandato
        eff_p    = lock_p if is_locked_day else (ov_p if ov_p else rec_p)
        market   = ddata.get("market_avg", 0)
        occ      = ddata.get("occupancy", 0)
        wknd     = ddata.get("is_weekend", False)
        has_evt  = ddata.get("has_event", False)
        evt_name = ddata.get("event_name", "")
        delta    = ddata.get("delta_vs_market", 0)
        conf     = ddata.get("confidence_score", 0)
        brkdwn   = ddata.get("breakdown", {})
        safety_note = ddata.get("safety_note", "")

        sign_m     = "+" if delta >= 0 else ""
        conf_color = "#2ecc71" if conf >= 0.7 else ("#f39c12" if conf >= 0.5 else "#e74c3c")
        wknd_str   = "🏖️ Weekend" if wknd else "📅 Feriale"
        evt_detail = f" | 🎉 {evt_name}" if has_evt else ""
        if is_locked_day:
            ov_note = " | 🔒 Bloccato"
        elif ov_p:
            ov_note = " | ✏️ Modifica"
        else:
            ov_note = ""

        with dc2:
            st.markdown(
                f'<div style="background:linear-gradient(135deg,#667eea,#764ba2);'
                f'border-radius:14px;padding:20px;color:white;text-align:center">'
                f'<div style="font-size:0.82rem;opacity:0.85;text-transform:uppercase">'
                f'{sel_day.strftime("%A, %d %B %Y")}</div>'
                f'<div style="font-size:2.5rem;font-weight:900;margin:8px 0">'
                f'&#8364;{eff_p:.0f}</div>'
                f'<div style="font-size:0.88rem;opacity:0.9">'
                f'vs Mercato: <b>{sign_m}{delta:.1f}%</b>'
                f'&nbsp;|&nbsp; Conf: '
                f'<b style="color:{conf_color}">{conf*100:.0f}%</b></div>'
                f'<div style="font-size:0.82rem;margin-top:6px;opacity:0.85">'
                f'{wknd_str}{evt_detail}&nbsp;|&nbsp; Occ: <b>{occ*100:.0f}%</b>'
                f'{ov_note}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        st.markdown("---")
        det1, det2 = st.columns([1, 1], gap="large")

        with det1:
            st.markdown("**📊 Contesto di Mercato**")
            dm1, dm2, dm3 = st.columns(3)
            dm1.metric("Media mercato",  f"&#8364;{market:.0f}")
            dm2.metric("Raccomandato",   f"&#8364;{rec_p:.0f}")
            dm3.metric("Occupazione",    f"{occ*100:.0f}%")

            # MPI calcolato localmente (raccomandato vs media mercato)
            _cal_mpi = round(rec_p / market * 100, 1) if market > 0 else None

            # Why block strutturato (sostituisce il raw dataframe)
            import json as _cj_cal
            st.markdown(
                _build_why_block(
                    factors=_cj_cal.dumps(brkdwn) if brkdwn else "",
                    safety_note=safety_note,
                    mpi=_cal_mpi,
                    conf=conf,
                    has_event=has_evt,
                    event_name=evt_name,
                    is_weekend=wknd,
                    occupancy=occ,
                ),
                unsafe_allow_html=True,
            )

        with det2:
            # ── Applica prezzo raccomandato (1-click) ──────────────────────
            if not is_locked_day:
                st.markdown("**⭐ Applica Prezzo Raccomandato**")
                _apply_label = f"✅ Usa €{rec_p:.0f} (suggerito dal motore)"
                if st.button(
                    _apply_label,
                    key=f"cal_apply_rec_{d_iso}",
                    use_container_width=True,
                    type="primary",
                ):
                    if f"cal_overrides_{sel_pid}" not in st.session_state:
                        st.session_state[f"cal_overrides_{sel_pid}"] = {}
                    st.session_state[f"cal_overrides_{sel_pid}"][d_iso] = float(rec_p)
                    upsert_calendar_price({
                        "account_id": current_account_id(),
                        "property_id": int(sel_pid),
                        "date": d_iso,
                        "current_price": float(rec_p),
                        "current_price_source": "manual_calendar",
                        "recommended_price": float(rec_p),
                        "status": "manual_override",
                        "notes": "Prezzo raccomandato confermato manualmente dal calendario.",
                    })
                    st.toast(
                        f"Prezzo raccomandato applicato: €{rec_p:.0f}",
                        icon="⭐",
                    )
                    st.rerun()
                st.markdown("---")

            st.markdown("**✏️ Modifica manuale**")
            st.caption(
                "Sostituisce il prezzo calcolato per questo giorno. "
                "Il resto del calendario non è influenzato."
            )
            st.caption("Nota: i limiti min/max proteggono solo i consigli automatici. Un prezzo inserito da te non viene limitato.")
            ov_input = st.number_input(
                "Prezzo personalizzato (€)",
                min_value=1.0,
                value=float(
                    lock_p if is_locked_day else (ov_p if ov_p else rec_p)
                ),
                step=5.0,
                key=f"cal_ov_inp_{d_iso}",
            )

            ov_c1, ov_c2 = st.columns(2)
            with ov_c1:
                if st.button("💾 Salva modifica", key=f"cal_ov_save_{d_iso}",
                             use_container_width=True, type="primary",
                             disabled=is_locked_day):
                    if f"cal_overrides_{sel_pid}" not in st.session_state:
                        st.session_state[f"cal_overrides_{sel_pid}"] = {}
                    st.session_state[f"cal_overrides_{sel_pid}"][d_iso] = float(ov_input)
                    upsert_calendar_price({
                        "account_id": current_account_id(),
                        "property_id": int(sel_pid),
                        "date": d_iso,
                        "current_price": float(ov_input),
                        "current_price_source": "manual_calendar",
                        "recommended_price": float(rec_p),
                        "status": "manual_override",
                        "notes": "Prezzo personalizzato impostato manualmente dal calendario.",
                    })
                    st.toast(
                        f"Modifica {sel_day.strftime('%d %b')} → €{ov_input:.0f}",
                        icon="✏️",
                    )
                    st.rerun()
            with ov_c2:
                if ov_p and not is_locked_day and st.button(
                    "🔄 Ripristina", key=f"cal_ov_del_{d_iso}",
                    use_container_width=True,
                ):
                    ovs = st.session_state.get(f"cal_overrides_{sel_pid}", {})
                    ovs.pop(d_iso, None)
                    st.session_state[f"cal_overrides_{sel_pid}"] = ovs
                    upsert_calendar_price({
                        "account_id": current_account_id(),
                        "property_id": int(sel_pid),
                        "date": d_iso,
                        "current_price": float(rec_p),
                        "current_price_source": "pricing_engine",
                        "recommended_price": float(rec_p),
                        "status": "recommended",
                        "notes": "Modifica manuale rimossa dal calendario.",
                    })
                    st.toast("Modifica rimossa", icon="🔄")
                    st.rerun()

            # ── Blocco prezzo ──────────────────────────────────────────────
            st.markdown("---")
            st.markdown("**🔒 Blocca Prezzo**")
            st.caption(
                "Impedisce al motore pricing di ricalcolare il prezzo per "
                "questo giorno. Ha la massima priorità su override e motore."
            )
            lock_key = f"cal_locks_{sel_pid}"
            if is_locked_day:
                st.info(
                    f"🔒 Prezzo bloccato a **€{lock_p:.0f}**  "
                    f"— il motore non ricalcolerà questo giorno."
                )
                if st.button(
                    "🔓 Sblocca Prezzo", key=f"cal_unlock_{d_iso}",
                    use_container_width=True,
                ):
                    st.session_state[lock_key].pop(d_iso, None)
                    upsert_calendar_price({
                        "account_id": current_account_id(),
                        "property_id": int(sel_pid),
                        "date": d_iso,
                        "current_price": float(ov_p if ov_p else rec_p),
                        "current_price_source": "pricing_engine",
                        "recommended_price": float(rec_p),
                        "status": "recommended",
                        "notes": "Blocco manuale rimosso dal calendario.",
                    })
                    st.toast(
                        f"Prezzo {sel_day.strftime('%d %b')} sbloccato",
                        icon="🔓",
                    )
                    st.rerun()
            else:
                lock_val = float(ov_input)   # usa il valore corrente nel campo
                if st.button(
                    f"🔒 Blocca a €{lock_val:.0f}", key=f"cal_lock_{d_iso}",
                    use_container_width=True,
                ):
                    if lock_key not in st.session_state:
                        st.session_state[lock_key] = {}
                    st.session_state[lock_key][d_iso] = lock_val
                    upsert_calendar_price({
                        "account_id": current_account_id(),
                        "property_id": int(sel_pid),
                        "date": d_iso,
                        "current_price": float(lock_val),
                        "current_price_source": "manual_lock",
                        "recommended_price": float(rec_p),
                        "status": "locked",
                        "notes": "Prezzo bloccato manualmente dal calendario.",
                    })
                    st.toast(
                        f"Prezzo {sel_day.strftime('%d %b')} bloccato a €{lock_val:.0f}",
                        icon="🔒",
                    )
                    st.rerun()

    # ── Riepilogo mensile ─────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown(
        '<div class="section-title">💰 Riepilogo Mensile</div>',
        unsafe_allow_html=True,
    )

    month_prices = [
        p for p in prices_list
        if p["date"].year == cal_year and p["date"].month == cal_month
    ]

    if not month_prices:
        st.info("Nessun dato per il mese selezionato.")
        return

    # Priorità: bloccato > override > raccomandato
    eff_prices = [
        locks.get(p["date_iso"],
                  overrides.get(p["date_iso"], p["recommended_price"]))
        for p in month_prices
    ]

    avg_price   = sum(eff_prices) / len(eff_prices)
    rev_est     = sum(ep * p["occupancy"] for ep, p in zip(eff_prices, month_prices))
    days_above  = sum(1 for p, ep in zip(month_prices, eff_prices)
                      if p["market_avg"] > 0 and ep > p["market_avg"] * 1.05)
    days_below  = sum(1 for p, ep in zip(month_prices, eff_prices)
                      if p["market_avg"] > 0 and ep < p["market_avg"] * 0.95)
    days_evt    = sum(1 for p in month_prices if p["has_event"])
    days_wknd   = sum(1 for p in month_prices if p["is_weekend"])
    days_ov     = sum(1 for p in month_prices if p["date_iso"] in overrides)
    days_locked = sum(1 for p in month_prices if p["date_iso"] in locks)

    rs1, rs2, rs3, rs4, rs5, rs6, rs7, rs8 = st.columns(8)
    rs1.metric("💰 Prezzo medio",    f"€{avg_price:.0f}")
    rs2.metric("📊 Revenue stimata", f"€{rev_est:.0f}")
    rs3.metric("📈 Gg. sopra mkt",   days_above)
    rs4.metric("📉 Gg. sotto mkt",   days_below)
    rs5.metric("🎉 Gg. evento",      days_evt)
    rs6.metric("🏖️ Gg. weekend",    days_wknd)
    rs7.metric("✏️ Modifiche",        days_ov)
    rs8.metric("🔒 Bloccati",        days_locked)

    # Grafico prezzi mese
    st.markdown(
        f'<div class="section-title">📈 Prezzi {sel_month_lbl}</div>',
        unsafe_allow_html=True,
    )

    chart_rows = []
    for p, ep in zip(month_prices, eff_prices):
        tipo = ("Bloccato" if p["date_iso"] in locks
                else ("Modifica" if p["date_iso"] in overrides
                      else ("Weekend" if p["is_weekend"]
                            else ("Evento" if p["has_event"] else "Normale"))))
        chart_rows.append({
            "Data":        p["date"].isoformat(),
            "Prezzo (€)":  ep,
            "Mercato (€)": p["market_avg"],
            "Tipo":        tipo,
        })

    df_ch = pd.DataFrame(chart_rows)

    color_map = {
        "Bloccato": "#9b59b6",
        "Modifica": "#27ae60",
        "Weekend":  "#e74c3c",
        "Evento":   "#f39c12",
        "Normale":  "#667eea",
    }
    bar_colors = [color_map.get(t, "#667eea") for t in df_ch["Tipo"]]

    fig_cal = go.Figure()
    fig_cal.add_trace(go.Scatter(
        x=df_ch["Data"], y=df_ch["Mercato (€)"],
        name="Media Mercato",
        line=dict(color="#9b59b6", dash="dot", width=2),
    ))
    fig_cal.add_trace(go.Bar(
        x=df_ch["Data"], y=df_ch["Prezzo (€)"],
        name="Prezzo",
        marker_color=bar_colors,
        text=df_ch["Prezzo (€)"].apply(lambda x: f"€{x:.0f}"),
        textposition="outside",
    ))
    fig_cal.update_layout(
        xaxis_title="Data", yaxis_title="Prezzo (€)",
        height=340, showlegend=True,
        plot_bgcolor="white", paper_bgcolor="white",
        hovermode="x unified", bargap=0.15,
        legend=dict(orientation="h", y=1.02),
    )
    st.plotly_chart(fig_cal, use_container_width=True, config=PLOTLY_CONFIG)


# ═════════════════════════════════════════════════════════════════════════════
# TAB: TELEGRAM  (1-click SaaS flow – no technical fields exposed)
# ═════════════════════════════════════════════════════════════════════════════

def tab_telegram():
    st.markdown('<div class="section-title">🔔 Notifiche Telegram</div>',
                unsafe_allow_html=True)

    # ── Descrizione funzione ──────────────────────────────────────────────────
    st.markdown(
        "**⚡ Ricevi consigli di pricing senza aprire la dashboard.** "
        "PricePilot usa il piano attivo per decidere cosa inviare: consigli nel Free, "
        "richieste di approvazione nel Plus e report operativi nel Pro."
    )

    col_desc, col_preview = st.columns([1, 1], gap="large")

    with col_desc:
        st.markdown("##### Perché usarlo")
        st.markdown("""
        - **💡 Ricevi consigli motivati** → prezzo attuale, prezzo suggerito e motivo
        - **🔥 Non perdere mai momenti ad alta domanda** → weekend, eventi, festività
        - **📈 Resta competitivo** → confronti con mercato e competitor simili
        - **💰 Ogni notifica è un'opportunità di ricavo** → agisci subito sulle tue OTA
        """)
        st.markdown("##### Come si usa")
        st.markdown("""
        1. **Collega Telegram** → clicca il bottone qui sotto
        2. **PricePilot legge il tuo piano attivo** → Free, Plus o Pro
        3. **Ricevi l'alert corretto** → consiglio, approvazione o report operativo
        4. **Agisci solo quando serve** → manuale nel Free, approvazione nel Plus, automatico nel Pro
        """)

    with col_preview:
        st.markdown("##### Esempio di messaggio Telegram")
        st.markdown(
            """
            <div class="tg-preview">
                <div class="tg-preview-header">✈️ PricePilot &nbsp;·&nbsp; Suggerimento Prezzo</div>
                <hr class="tg-preview-divider">
                <div class="tg-preview-row">
                    <span class="tg-preview-label">🏠 Proprietà</span>
                    <span class="tg-preview-value">Villa Lago Como</span>
                </div>
                <div class="tg-preview-row">
                    <span class="tg-preview-label">💰 Prezzo attuale</span>
                    <span class="tg-preview-value">€ 80</span>
                </div>
                <div class="tg-preview-row">
                    <span class="tg-preview-label">🎯 Prezzo suggerito</span>
                    <span class="tg-preview-value">€ 95 &nbsp;<span style="color:#2ecc71;font-size:0.82rem">(+18.8%)</span></span>
                </div>
                <div class="tg-preview-row">
                    <span class="tg-preview-label">📊 Media mercato</span>
                    <span class="tg-preview-value">€ 88</span>
                </div>
                <div class="tg-preview-row">
                    <span class="tg-preview-label">📅 Motivo</span>
                    <span class="tg-preview-value">Weekend + Alta domanda</span>
                </div>
                <hr class="tg-preview-divider">
                <div class="tg-preview-question">Piano Free: aggiorna manualmente il prezzo sulle tue OTA.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown("---")

    props = list_properties(account_id=current_account_id())
    if not props:
        st.warning("Crea prima una proprietà nella tab **🏠 Proprietà**.")
        return

    # ── Carica modulo bot (silenzioso) ─────────────────────────────────────────
    try:
        from pricepilot.services.telegram_bot import is_configured, create_property_link
        bot_ready = is_configured()
    except Exception:
        bot_ready = False
        create_property_link = None

    # ── Card per ogni proprietà ────────────────────────────────────────────────
    for prop in props:
        prop_id = prop["id"]
        try:
            from pricepilot.core.database import get_telegram_link_by_property
            link = get_telegram_link_by_property(prop_id)
        except Exception:
            link = None

        is_connected = bool(link and link.get("chat_id") and link.get("active"))
        mode         = effective_sync_mode(get_effective_plan_for_property(prop), prop.get("sync_mode", "advisory"))
        mode_labels  = {"advisory": "💡 Manuale", "approval": "✅ Approvazione", "auto": "🤖 Automatico"}

        # ── Card container ────────────────────────────────────────────────────
        bg   = "#f0fdf4" if is_connected else "#fff9f0"
        brd  = "#86efac" if is_connected else "#fed7aa"
        _tg_status_html = (
            '<span style="color:#16a34a;font-weight:600">✅ Collegato</span>'
            if is_connected else
            '<span style="color:#dc2626;font-weight:600">🔴 Non collegato</span>'
        )
        _tg_user_html = (
            f'<div style="font-size:0.85rem;color:#555;margin-top:6px">'
            f'👤 @{link["telegram_username"] or "utente"}</div>'
            if is_connected and link else ''
        )
        _prop_name_esc = _html.escape(str(prop['name']))
        _mode_label_esc = _html.escape(str(mode_labels.get(mode, mode)))
        st.markdown(
            f'<div style="background:{bg};border:1.5px solid {brd};border-radius:12px;'
            f'padding:16px 20px;margin-bottom:14px">'
            f'<div style="display:flex;justify-content:space-between;align-items:center">'
            f'<div>'
            f'<span style="font-size:1.05rem;font-weight:700">{_prop_name_esc}</span>'
            f'&nbsp;<span style="font-size:0.8rem;color:#888">{_mode_label_esc}</span>'
            f'</div>'
            f'<div>{_tg_status_html}</div>'
            f'</div>'
            f'{_tg_user_html}'
            f'</div>',
            unsafe_allow_html=True,
        )

        col_a, col_b, col_c = st.columns([2, 1, 1])

        if is_connected:
            # ── Già collegato: mostra "Scollega" ─────────────────────────────
            with col_a:
                st.caption("Le notifiche di pricing vengono inviate su Telegram.")
            with col_b:
                if st.button("Invia test", key=f"tg_test_{prop_id}", use_container_width=True):
                    try:
                        with st.spinner("Invio consiglio Telegram..."):
                            process_decision(
                                property_id=prop_id,
                                occupancy=0.65,
                                target_date=date.today(),
                                force_mode="advisory",
                            )
                        st.toast("Consiglio Telegram inviato.", icon="✅")
                    except Exception as exc:
                        st.error(f"Errore invio test: {exc}")
            with col_c:
                if st.button("🔓 Scollega", key=f"rev_{prop_id}", use_container_width=True):
                    revoke_telegram_link(prop_id)
                    for k in list(st.session_state.keys()):
                        if k.startswith(f"tg_link_{prop_id}"):
                            del st.session_state[k]
                    st.success("Collegamento rimosso.")
                    st.rerun()

        else:
            # ── Non collegato: bottone "Collega Telegram" ─────────────────────
            with col_a:
                st.caption("Clicca il pulsante per generare il link di connessione Telegram.")
            with col_b:
                connect_btn = st.button(
                    "📱 Connetti Telegram",
                    key=f"conn_{prop_id}",
                    use_container_width=True,
                    type="primary",
                )

            if connect_btn:
                with st.spinner("Generazione link..."):
                    try:
                        # Prova prima con il modulo bot (se configurato)
                        if bot_ready and create_property_link:
                            li = create_property_link(prop_id)
                        else:
                            # Fallback: genera il token direttamente senza bot token
                            import secrets as _sec
                            _token = f"connect_{prop_id}_{_sec.token_hex(8)}"
                            from pricepilot.core.database import (
                                revoke_telegram_link as _rev,
                                save_telegram_link   as _stl,
                            )
                            _rev(prop_id)
                            _lid = _stl({
                                "property_id": prop_id,
                                "token":       _token,
                                "active":      1,
                            })
                            _bot_user = os.environ.get(
                                "TELEGRAM_BOT_USERNAME", "PricePilotBot"
                            ).strip().lstrip("@")
                            li = {
                                "link_id":     _lid,
                                "token":       _token,
                                "deep_link":   f"https://t.me/{_bot_user}?start={_token}",
                                "property_id": prop_id,
                            }
                        st.session_state[f"tg_link_{prop_id}"] = li
                    except Exception as exc:
                        st.error(f"Errore nella generazione del link: {exc}")

            # Mostra il link appena generato (session_state)
            if f"tg_link_{prop_id}" in st.session_state:
                li        = st.session_state[f"tg_link_{prop_id}"]
                deep_link = li["deep_link"]
                st.markdown(
                    f"""<div style="background:#eff6ff;border:1px solid #bfdbfe;
                        border-radius:10px;padding:14px 18px;margin-top:8px">
                        <div style="font-weight:600;margin-bottom:8px">
                            📲 Apri questo link su Telegram
                        </div>
                        <a href="{deep_link}" target="_blank"
                           style="display:inline-block;background:#0088cc;color:white;
                                  text-decoration:none;padding:10px 22px;border-radius:8px;
                                  font-weight:600;font-size:0.95rem">
                           ✈️ Apri in Telegram
                        </a>
                        <div style="font-size:0.82rem;color:#64748b;margin-top:10px">
                            Oppure copia il link: <code style="font-size:0.8rem">{deep_link}</code>
                        </div>
                        <div style="font-size:0.82rem;color:#475569;margin-top:8px">
                            1. Clicca <b>Apri in Telegram</b> &nbsp;·&nbsp;
                            2. Premi <b>START</b> &nbsp;·&nbsp;
                            3. Il bot conferma il collegamento
                        </div>
                    </div>""",
                    unsafe_allow_html=True,
                )

    st.markdown("---")

    # ── Approvazioni in sospeso ────────────────────────────────────────────────
    st.markdown('<div class="section-title">⏳ Approvazioni in Sospeso</div>',
                unsafe_allow_html=True)
    st.caption("Decisioni in modalità Approvazione che attendono conferma manuale.")

    try:
        pending   = get_pending_approvals(account_id=current_account_id())
        all_props = props
    except Exception:
        pending   = []
        all_props = props

    if pending:
        from pricepilot.engine.decision_engine import approve_decision
        from pricepilot.core.database import get_conn as _get_conn

        for p in pending:
            pct      = (p["new_price"] - p["old_price"]) / max(p["old_price"], 1) * 100
            arrow    = "▲" if pct > 0 else "▼"
            pname    = next((pr["name"] for pr in all_props if pr["id"] == p["property_id"]),
                            f"#{p['property_id']}")
            ts       = p.get("timestamp", "")[:16]
            occ      = p.get("occupancy") or 0
            notes_p  = p.get("notes", "")

            # Estrai conf e event dai notes
            _conf_vt  = None
            _evt_vt   = ""
            for _xpt in notes_p.split("|"):
                _xpt = _xpt.strip()
                if _xpt.startswith("conf="):
                    try: _conf_vt = float(_xpt.split("=",1)[1])
                    except Exception: pass
                elif _xpt.startswith("event="):
                    _evt_vt = _xpt.split("=",1)[1].strip()

            ca, cb, cc = st.columns([4, 1, 1])
            with ca:
                pct_col_t = "#155724" if pct > 0 else "#721c24"
                st.markdown(
                    f"<div style='font-size:0.95rem'>"
                    f"<b>{pname}</b> &nbsp;·&nbsp; "
                    f"€{p['old_price']:.2f} → "
                    f"<span style='color:{pct_col_t};font-weight:700'>€{p['new_price']:.2f}</span> "
                    f"({arrow} {abs(pct):.1f}%) &nbsp;·&nbsp; "
                    f"Occ: {int(occ*100)}% &nbsp;·&nbsp; <code>{ts}</code>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                st.markdown(
                    _build_why_block(
                        notes=notes_p,
                        factors=p.get("factors") or "",
                        mpi=p.get("mpi"),
                        conf=_conf_vt,
                        has_event=bool(_evt_vt and _evt_vt != "none"),
                        event_name=_evt_vt,
                        occupancy=occ,
                    ),
                    unsafe_allow_html=True,
                )
            with cb:
                if st.button("✅", key=f"app_{p['id']}", help="Approva", use_container_width=True):
                    approve_decision(p["id"], account_id=current_account_id())
                    st.toast("Approvato. Aggiorna manualmente il prezzo sul canale.", icon="✅")
                    st.rerun()
            with cc:
                if st.button("❌", key=f"rej_{p['id']}", help="Rifiuta", use_container_width=True):
                    with _get_conn() as conn:
                        conn.execute(
                            "UPDATE decision_log SET applied=0, "
                            "decision=decision||' [REJECTED]' WHERE id=? AND account_id=?",
                            (p["id"], current_account_id())
                        )
                    st.toast("Rifiutato.", icon="❌")
                    st.rerun()
            st.divider()
    else:
        st.success("✅ Nessuna approvazione in sospeso.")


# ═════════════════════════════════════════════════════════════════════════════
# TAB: INTEGRATIONS
# ═════════════════════════════════════════════════════════════════════════════

def tab_integrations():
    st.markdown('<div class="section-title">🔌 Integrazioni OTA</div>',
                unsafe_allow_html=True)
    st.markdown(
        "**Attiva il pricing automatico → i prezzi si aggiornano da soli su Airbnb e Booking.com.** "
        "Nessun lavoro manuale, nessuna opportunità persa. "
        "In modalità **🤖 Automatico** ogni variazione di mercato si traduce in un prezzo aggiornato in tempo reale."
    )
    st.markdown(
        '<div style="background:#f0fdf4;border:1px solid #86efac;border-radius:10px;'
        'padding:12px 18px;margin:8px 0 16px;font-size:0.85rem;color:#166534;">'
        '💡 <b>Massimizza i ricavi senza lavorare di più</b> — '
        'le integrazioni OTA ti permettono di catturare ogni picco di domanda automaticamente.'
        '</div>',
        unsafe_allow_html=True,
    )

    props = list_properties(account_id=current_account_id())
    if not props:
        st.warning("Crea prima una proprietà nella tab 🏠 Proprietà.")
        return

    import os
    try:
        from pricepilot.integrations.channel_manager import get_channel_manager
        cm = get_channel_manager()
    except Exception as exc:
        st.error(f"Channel Manager non disponibile: {exc}")
        return

    from pricepilot.core.database import get_conn

    # ── Configurazione globale piattaforme ────────────────────────────────────
    _PLATFORM_META = {
        "airbnb":  {"label": "Airbnb",           "icon": "🏠", "color": "#FF5A5F",
                    "configured": bool(os.environ.get("AIRBNB_API_TOKEN"))},
        "booking": {"label": "Booking.com",       "icon": "🌐", "color": "#003580",
                    "configured": bool(os.environ.get("BOOKING_API_KEY"))},
        "vrbo":    {"label": "Vrbo",              "icon": "🏡", "color": "#3D5A80",
                    "configured": bool(os.environ.get("VRBO_API_KEY"))},
        "direct":  {"label": "Sito diretto",       "icon": "🔗", "color": "#6366f1",
                    "configured": False,
                    "desc": "Sincronizza prezzi con il tuo sito di prenotazione diretta."},
        "channel_manager": {"label": "Channel Manager", "icon": "🔄", "color": "#0ea5e9",
                    "configured": False,
                    "desc": "Collega servizi come Guesty, Hostaway o Lodgify."},
    }

    # ── Card per proprietà ────────────────────────────────────────────────────
    st.markdown("#### 🏠 Le Tue Proprietà")

    for prop in props:
        status      = cm.get_status(prop)
        mode        = prop.get("sync_mode", "advisory")
        mode_colors = {"advisory": "#6366f1", "approval": "#f59e0b", "auto": "#10b981"}
        mode_labels = {"advisory": "💡 Manuale", "approval": "✅ Approvazione", "auto": "🤖 Automatico"}
        mode_color  = mode_colors.get(mode, "#9ca3af")
        mode_label  = mode_labels.get(mode, mode)

        if status["is_real"]:
            conn_badge = "🟢 Attivo";        conn_bg = "#dcfce7"; conn_fg = "#166534"
        elif status["supported"]:
            conn_badge = "🟡 Configurato";  conn_bg = "#fef9c3"; conn_fg = "#854d0e"
        else:
            conn_badge = "🔴 Non connesso"; conn_bg = "#fee2e2"; conn_fg = "#991b1b"

        # Ultimo aggiornamento prezzo
        try:
            with get_conn() as conn:
                last_upd = conn.execute(
                    "SELECT new_price, applied_at, is_stub FROM price_updates "
                    "WHERE property_id=? ORDER BY applied_at DESC LIMIT 1",
                    (prop["id"],)
                ).fetchone()
        except Exception:
            last_upd = None

        last_str = (
            f"Ultimo: €{last_upd[0]:.2f} · {str(last_upd[1])[:16]}"
            f"{'  ·  sim.' if last_upd[2] else '  ·  live'}"
            if last_upd else "Nessun aggiornamento"
        )

        # Piattaforma della proprietà
        plat_key  = prop.get("platform", "other")
        plat_meta = _PLATFORM_META.get(plat_key, {"label": plat_key.upper(), "icon": "🔗", "color": "#6b7280"})
        plat_configured = _PLATFORM_META.get(plat_key, {}).get("configured", False)
        plat_status_dot = "🟢" if plat_configured else "⚪"

        with st.container():
            card_col, btn_col = st.columns([4, 1])
            with card_col:
                st.markdown(
                    f"""<div style="background:white;border:1.5px solid #e5e7eb;border-radius:14px;
                        padding:16px 20px;margin-bottom:8px;box-shadow:0 2px 6px rgba(0,0,0,.04)">
                        <div style="display:flex;justify-content:space-between;align-items:flex-start">
                          <div>
                            <span style="font-size:1.05rem;font-weight:800">{prop['name']}</span>
                            &nbsp;&nbsp;
                            <span style="background:{mode_color};color:white;padding:2px 10px;
                                   border-radius:20px;font-size:0.75rem;font-weight:600">
                                {mode_label}
                            </span>
                            <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                                <span style="background:#f8fafc;border:1px solid #e2e8f0;
                                       border-radius:20px;padding:3px 10px;font-size:0.8rem">
                                    {plat_status_dot} {plat_meta['icon']} {plat_meta['label']}
                                </span>
                                <span style="background:{conn_bg};color:{conn_fg};
                                       border-radius:20px;padding:3px 10px;font-size:0.8rem;font-weight:600">
                                    {conn_badge}
                                </span>
                                <span style="color:#9ca3af;font-size:0.78rem">
                                    ID: <code>{prop.get('listing_id') or '—'}</code>
                                </span>
                            </div>
                            <div style="font-size:0.78rem;color:#9ca3af;margin-top:6px">
                                💰 €{prop['min_price']:.0f} – €{prop['max_price']:.0f}
                                &nbsp;·&nbsp; {last_str}
                            </div>
                          </div>
                        </div>
                    </div>""",
                    unsafe_allow_html=True,
                )
            with btn_col:
                st.markdown("<br>", unsafe_allow_html=True)
                if not plat_configured:
                    if st.button(
                        "🔗 Configura", key=f"integ_connect_{prop['id']}",
                        use_container_width=True,
                        help=f"Imposta le credenziali API per {plat_meta['label']} nel file .env",
                    ):
                        st.session_state[f"integ_show_help_{prop['id']}"] = True
                else:
                    st.markdown(
                        f'<div style="text-align:center;padding:8px;font-size:0.82rem;'
                        f'color:#166534;font-weight:600">✅ Connesso</div>',
                        unsafe_allow_html=True,
                    )
                # Mostra mini-guida se richiesta
                if st.session_state.get(f"integ_show_help_{prop['id']}"):
                    st.info(
                        f"Per connettere **{plat_meta['label']}** imposta le "
                        f"variabili richieste nel file `.env` e riavvia PricePilot. "
                        f"Vedi le istruzioni in fondo alla pagina.",
                        icon="ℹ️",
                    )

    st.markdown("---")

    # ── Riepilogo piattaforme globali ─────────────────────────────────────────
    st.markdown("#### 🌍 Stato Credenziali API")
    gcols = st.columns(len(_PLATFORM_META))
    for idx, (pkey, info) in enumerate(_PLATFORM_META.items()):
        with gcols[idx]:
            if info["configured"]:
                b_label, b_bg, b_fg = "🟢 Configurate", "#dcfce7", "#166534"
                detail = info.get("desc", "Credenziali presenti in .env")
            else:
                b_label, b_bg, b_fg = "⚪ Non configurato", "#f9fafb", "#4b5563"
                detail = info.get("desc", "Aggiungi le variabili in .env")
            st.markdown(
                f"""<div style="background:white;border:1px solid #e5e7eb;border-radius:12px;
                    padding:14px 16px;text-align:center">
                    <div style="font-size:1.8rem">{info['icon']}</div>
                    <div style="font-weight:700;margin:4px 0">{info['label']}</div>
                    <span style="background:{b_bg};color:{b_fg};padding:3px 10px;
                          border-radius:20px;font-size:0.8rem;font-weight:600">{b_label}</span>
                    <div style="font-size:0.75rem;color:#9ca3af;margin-top:6px">{detail}</div>
                </div>""",
                unsafe_allow_html=True,
            )

    st.markdown("---")

    # ── Istruzioni configurazione ──────────────────────────────────────────────
    with st.expander("⚙️ Come attivare le integrazioni Live"):
        st.markdown("""
**Airbnb** (richiede accesso [Airbnb Channel Manager API](https://developers.airbnb.com))
```
AIRBNB_API_TOKEN=<token OAuth2>
AIRBNB_LISTING_ID=<id numerico listing>
```

**Booking.com** (richiede [Connectivity Partner Agreement](https://partner.booking.com))
```
BOOKING_API_KEY=<api_key>
BOOKING_HOTEL_ID=<hotel_id>
BOOKING_ROOM_ID=<room_id>
```

**Vrbo**
```
VRBO_API_KEY=<api_key>
```

Dopo aver aggiunto le credenziali nel file `.env`:
1. Imposta il **Listing ID** nel form della proprietà (tab 🏠 Proprietà)
2. Imposta la modalità su **🤖 Auto**
3. Il badge cambierà da ⚪ Non configurate a 🟢 Configurate
""")


# ═════════════════════════════════════════════════════════════════════════════
# TAB: AUTO APPLY LOG
# ═════════════════════════════════════════════════════════════════════════════

def tab_auto_log():
    st.markdown('<div class="section-title">🤖 Auto Apply – Storico Prezzi Applicati</div>',
                unsafe_allow_html=True)
    st.markdown(
        "Storico dei prezzi applicati automaticamente dalla modalità **🤖 Auto Apply**. "
        "I record con badge 🟡 Sim. sono stati simulati (non inviati alla piattaforma reale)."
    )

    props = list_properties(account_id=current_account_id())
    prop_map = {p["id"]: p["name"] for p in props}
    prop_ids = [int(p["id"]) for p in props]
    if not prop_ids:
        st.info("Nessuna proprieta configurata.")
        return

    # ── Filtri ────────────────────────────────────────────────────────────────
    col_f1, col_f2, col_f3 = st.columns(3)
    with col_f1:
        filter_prop = st.selectbox(
            "Filtra proprietà",
            options=["Tutte"] + [p["name"] for p in props],
            key="alog_prop",
        )
    with col_f2:
        filter_mode = st.selectbox(
            "Tipo aggiornamento",
            ["Tutti", "🟢 Reale", "🟡 Simulato"],
            key="alog_mode",
        )
    with col_f3:
        limit_rows = st.selectbox("Righe", [50, 100, 200, 500], key="alog_limit")

    # ── Leggi dati da price_updates + decision_log (per motivo) ──────────────
    from pricepilot.core.database import get_conn
    try:
        with get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS price_updates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    property_id INTEGER,
                    platform TEXT,
                    listing_id TEXT,
                    target_date TEXT,
                    new_price REAL,
                    ok INTEGER,
                    error TEXT,
                    applied_at TEXT,
                    is_stub INTEGER DEFAULT 1
                )
            """)
            # Cerca di unire con decision_log per avere il motivo.
            # Filtra sempre sulle proprieta dell'account corrente.
            placeholders = ",".join("?" for _ in prop_ids)
            rows = conn.execute(f"""
                SELECT
                    pu.id, pu.property_id, pu.platform, pu.target_date,
                    pu.new_price, pu.ok, pu.is_stub, pu.applied_at,
                    p.name AS property_name,
                    dl.old_price,
                    dl.notes AS reason
                FROM price_updates pu
                LEFT JOIN properties p ON p.id = pu.property_id
                LEFT JOIN decision_log dl
                    ON dl.property_id = pu.property_id
                    AND dl.applied = 1
                    AND date(dl.timestamp) = date(pu.applied_at)
                WHERE pu.property_id IN ({placeholders})
                ORDER BY pu.applied_at DESC
                LIMIT ?
            """, (*prop_ids, int(limit_rows))).fetchall()
    except Exception as exc:
        st.error(f"Errore lettura log: {exc}")
        return

    if not rows:
        st.markdown(
            '<div class="alert-blue">ℹ️ Nessun aggiornamento automatico registrato. '
            'Diventa attivo quando una proprietà in modalità <b>🤖 Auto</b> '
            'esegue la prima decisione.</div>',
            unsafe_allow_html=True,
        )
        return

    df = pd.DataFrame([dict(r) for r in rows])

    # Applica filtri
    if filter_prop != "Tutte":
        df = df[df["property_name"] == filter_prop]
    if filter_mode == "🟢 Reale":
        df = df[df["is_stub"] == 0]
    elif filter_mode == "🟡 Simulato":
        df = df[df["is_stub"] == 1]

    if df.empty:
        st.info("Nessun record per i filtri selezionati.")
        return

    # ── KPI rapide ────────────────────────────────────────────────────────────
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("📦 Aggiornamenti totali", len(df))
    k2.metric("🟢 Reali",     int((df["is_stub"] == 0).sum()))
    k3.metric("🟡 Simulati",  int((df["is_stub"] == 1).sum()))
    k4.metric("💰 Prezzo medio", f"€{df['new_price'].mean():.2f}" if not df.empty else "—")

    st.markdown("---")

    # ── Visualizzazione card per ogni riga ────────────────────────────────────
    for _, row in df.iterrows():
        old_p    = float(row.get("old_price") or 0)
        new_p    = float(row.get("new_price") or 0)
        pct      = ((new_p - old_p) / max(old_p, 1)) * 100 if old_p > 0 else 0
        arrow    = "▲" if pct > 1 else ("▼" if pct < -1 else "→")
        pct_col  = "#155724" if pct > 0 else ("#721c24" if pct < 0 else "#555")
        is_live  = row.get("is_stub", 1) == 0
        ok       = row.get("ok", 0) == 1
        ts       = str(row.get("applied_at", ""))[:16]
        pname    = row.get("property_name", "—")
        platform = str(row.get("platform", "—")).upper()

        # Motivo (da decision_log)
        reason_raw   = str(row.get("reason", "") or "")
        reason_parts = [
            p.strip() for p in reason_raw.split("|")
            if p.strip() and not p.strip().startswith("conf=")
        ]
        reason_display = " · ".join(reason_parts[:3]) if reason_parts else "—"

        type_badge = (
            '<span class="badge-green">🟢 Reale</span>'
            if is_live else
            '<span class="badge-yellow">🟡 Simulato</span>'
        )
        ok_badge = (
            '<span class="badge-green">✅ OK</span>'
            if ok else
            '<span class="badge-red">❌ Errore</span>'
        )

        price_str = (
            f'€{old_p:.2f} → <span style="color:{pct_col};font-weight:700">€{new_p:.2f}</span>'
            f' <span style="color:{pct_col};font-size:0.82rem">({arrow} {abs(pct):.1f}%)</span>'
            if old_p > 0 else
            f'<span style="font-weight:700">€{new_p:.2f}</span>'
        )

        st.markdown(
            f'<div style="background:white;border:1px solid #e8e8e8;border-radius:10px;'
            f'padding:12px 18px;margin-bottom:8px;box-shadow:0 1px 4px rgba(0,0,0,.04)">'
            f'<div style="display:flex;align-items:flex-start;justify-content:space-between">'
            f'<div style="flex:1">'
            f'<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">'
            f'<span style="font-weight:700;font-size:0.95rem">{pname}</span>'
            f'<span style="font-size:0.8rem;color:#888">📅 {ts}</span>'
            f'<span style="font-size:0.8rem;color:#888">🌐 {platform}</span>'
            f'</div>'
            f'<div style="font-size:0.95rem;margin:5px 0">{price_str}</div>'
            f'<div style="font-size:0.78rem;color:#888">'
            f'💡 <b>Motivo:</b> {reason_display}'
            f'</div>'
            f'</div>'
            f'<div style="margin-left:16px;flex-shrink:0;display:flex;gap:6px;flex-wrap:wrap">'
            f'{type_badge} {ok_badge}'
            f'</div>'
            f'</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # ── Grafico: prezzi nel tempo ─────────────────────────────────────────────
    if len(df) > 1:
        df["applied_at_dt"] = pd.to_datetime(df["applied_at"].str[:16], errors="coerce")
        df_chart = df.dropna(subset=["applied_at_dt"]).sort_values("applied_at_dt")
        if not df_chart.empty:
            fig = px.scatter(
                df_chart,
                x="applied_at_dt", y="new_price",
                color="property_name",
                symbol=df_chart["is_stub"].map({0: "circle", 1: "x"}),
                title="Prezzi Auto-Applicati nel Tempo",
                labels={
                    "applied_at_dt": "Data applicazione",
                    "new_price": "Prezzo (€)",
                    "property_name": "Proprietà",
                },
                color_discrete_sequence=px.colors.qualitative.Set2,
            )
            fig.update_layout(
                height=340, plot_bgcolor="white", paper_bgcolor="white",
                hovermode="x unified",
            )
            st.plotly_chart(fig, use_container_width=True, config=PLOTLY_CONFIG)


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    if not require_auth():
        return

    reset_account_scoped_state_if_needed()
    cfg = render_sidebar()
    account_id = current_account_id()

    # ── Header ────────────────────────────────────────────────────────────────
    active_prop_id   = st.session_state.get("active_prop_id")
    active_prop_name = ""
    if active_prop_id:
        try:
            p = get_property_by_id(active_prop_id, account_id=account_id)
            if p:
                active_prop_name = p.get("name", "")
            else:
                st.session_state.pop("active_prop_id", None)
        except Exception:
            pass

    subtitle = (
        f"Dynamic Pricing · <span style='color:#6366f1;font-weight:600'>"
        f"{active_prop_name}</span>"
        if active_prop_name
        else "Dynamic Pricing per Affitti Brevi"
    )
    st.markdown(
        f'<h1 class="main-title">✈️ PricePilot</h1>'
        f'<p class="subtitle">{subtitle}</p>',
        unsafe_allow_html=True,
    )

    render_logout_button()

    # ── KPI mini-bar in header (solo se ci sono dati) ─────────────────────────
    try:
        stats = get_summary_stats(account_id=account_id)
        props_count = len(list_properties(account_id=account_id))
        if stats["total_decisions"] > 0 or props_count > 0:
            _avg_p  = stats.get("avg_price", 0) or 0
            _chg    = stats.get("avg_change_pct", 0) or 0

            # Ricavi stimati mese = prezzo medio × 30 notti
            _rev_month  = _avg_p * 30

            # Guadagno perso = stima della riduzione applicata ai prezzi
            _lost       = abs(min(0.0, _chg) / 100) * _rev_month

            # Opportunità = potenziale +10% sul prezzo attuale × 30 notti
            _upside     = _avg_p * 0.10 * 30

            # Build delta pill labels
            _chg_lbl   = f"{_chg:+.1f}% ottimizzazione" if _chg else "nessuna variazione"
            _lost_lbl  = "stai perdendo questo" if _lost > 0 else "✓ nessuna perdita"
            _up_lbl    = "puoi guadagnare di più"
            _prop_lbl  = f"{props_count} propert{'à' if props_count == 1 else 'à'} attiv{'a' if props_count == 1 else 'e'}"

            st.markdown(
                '<div class="rev-kpi-wrap">'

                # Card 1 — Ricavi stimati (primary, verde)
                '<div class="rev-kpi-card primary">'
                '<div class="rev-kpi-icon">💰</div>'
                '<p class="rev-kpi-label">Ricavi stimati (mese)</p>'
                f'<p class="rev-kpi-value">€{_rev_month:,.0f}</p>'
                f'<span class="rev-kpi-delta">{_chg_lbl}</span>'
                '<p class="rev-kpi-desc">Basato sulla strategia di pricing attuale</p>'
                '</div>'

                # Card 2 — Guadagno perso (danger, rosso)
                '<div class="rev-kpi-card danger">'
                '<div class="rev-kpi-icon">🔴</div>'
                '<p class="rev-kpi-label">Guadagno perso</p>'
                f'<p class="rev-kpi-value">€{_lost:,.0f}</p>'
                f'<span class="rev-kpi-delta">{_lost_lbl}</span>'
                '<p class="rev-kpi-desc">Stai prezzando sotto la domanda di mercato</p>'
                '</div>'

                # Card 3 — Opportunità (warning, giallo)
                '<div class="rev-kpi-card warning">'
                '<div class="rev-kpi-icon">⚡</div>'
                '<p class="rev-kpi-label">Opportunità di guadagno</p>'
                f'<p class="rev-kpi-value">€{_upside:,.0f}</p>'
                f'<span class="rev-kpi-delta">{_up_lbl}</span>'
                '<p class="rev-kpi-desc">Potresti guadagnare di più ottimizzando i prezzi</p>'
                '</div>'

                # Card 4 — Proprietà (neutral)
                '<div class="rev-kpi-card neutral">'
                '<div class="rev-kpi-icon">🏠</div>'
                '<p class="rev-kpi-label">Proprietà attive</p>'
                f'<p class="rev-kpi-value">{props_count}</p>'
                f'<span class="rev-kpi-delta">{_prop_lbl}</span>'
                '<p class="rev-kpi-desc">monitorate in tempo reale</p>'
                '</div>'

                '</div>',
                unsafe_allow_html=True,
            )

            # ── Insight banner dinamico ───────────────────────────────────────
            try:
                if _lost > 50:
                    _ins_icon = "⚠️"
                    _ins_color = "#fef2f2"
                    _ins_border = "#fca5a5"
                    _ins_text_color = "#991b1b"
                    _ins_msg = (
                        f"Stai perdendo circa <b>€{_lost:,.0f}/mese</b> "
                        f"prezzando sotto la domanda di mercato → "
                        f"alzare i prezzi del {abs(_chg):.0f}% recupererebbe questo guadagno."
                    )
                elif _upside > 0 and _chg >= 0:
                    _ins_icon = "📈"
                    _ins_color = "#fefce8"
                    _ins_border = "#fde68a"
                    _ins_text_color = "#92400e"
                    _ins_msg = (
                        f"C'è un'opportunità di <b>€{_upside:,.0f}/mese</b> non ancora catturata → "
                        f"ottimizzare la strategia di prezzo può aumentare i tuoi ricavi del 10% o più."
                    )
                elif _chg > 0:
                    _ins_icon = "✅"
                    _ins_color = "#f0fdf4"
                    _ins_border = "#86efac"
                    _ins_text_color = "#166534"
                    _ins_msg = (
                        f"I prezzi sono stati ottimizzati al rialzo del <b>{_chg:.1f}%</b> — "
                        f"stai massimizzando i ricavi rispetto al periodo base."
                    )
                else:
                    _ins_icon = "💡"
                    _ins_color = "#f8fafc"
                    _ins_border = "#cbd5e1"
                    _ins_text_color = "#475569"
                    _ins_msg = "Aggiungi una proprietà e avvia il motore per vedere le opportunità di ricavo."

                st.markdown(
                    f'<div style="background:{_ins_color};border:1px solid {_ins_border};'
                    f'border-radius:12px;padding:14px 20px;margin:8px 0 4px;'
                    f'color:{_ins_text_color};font-size:0.88rem;line-height:1.5;">'
                    f'{_ins_icon}&nbsp;&nbsp;{_ins_msg}'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            except Exception:
                pass
    except Exception:
        pass

    st.markdown("---")

    # ── Tabs SaaS ─────────────────────────────────────────────────────────────
    tab0, tab1, tab2, tab_cal, tab3, tab4, tab5, tab6, tab7 = st.tabs([
        "🏠 Home",
        "🏡 Proprietà",
        "🎯 Prezzi",
        "📅 Calendario",
        "📈 Analisi",
        "📋 Decisioni",
        "🔔 Telegram",
        "🔌 Integrazioni",
        "🤖 Auto Apply",
    ])

    with tab0:
        tab_home(cfg)
    with tab1:
        tab_properties()
    with tab2:
        tab_pricing(cfg)
    with tab_cal:
        tab_calendar(cfg)
    with tab3:
        tab_analytics(cfg)
    with tab4:
        tab_decisions(cfg)
    with tab5:
        tab_telegram()
    with tab6:
        tab_integrations()
    with tab7:
        tab_auto_log()


if __name__ == "__main__":
    main()
