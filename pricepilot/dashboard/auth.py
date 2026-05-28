"""
PricePilot dashboard authentication.

Default mode is local SQLite auth so the app can be tested without external
services. Supabase is used automatically when SUPABASE_URL and SUPABASE_ANON_KEY
are configured. Set PRICEPILOT_AUTH_MODE=disabled only for explicit dev bypass.
"""
from __future__ import annotations

import hashlib
import html as _html
import os
import secrets
from datetime import datetime

import streamlit as st
import streamlit.components.v1 as components

from pricepilot.core.database import (
    create_account,
    create_user,
    get_user_by_email,
    update_user,
)
from pricepilot.core.plans import get_plan, normalize_plan
from pricepilot.core.supabase_client import get_supabase_client
from pricepilot.services.account_service import create_account_owner

_KEY_USER = "pp_auth_user"
_KEY_SESSION = "pp_auth_session"
_KEY_PUBLIC_VIEW = "pp_public_view"
_KEY_SELECTED_PLAN = "pp_selected_plan"

PUBLIC_VIEWS = {"landing", "login", "register", "forgot"}
PLAN_ORDER = ("free", "plus", "pro")


def _get_client():
    return get_supabase_client()


def get_current_user() -> dict | None:
    return st.session_state.get(_KEY_USER)


def get_current_account_id() -> int:
    user = get_current_user() or {}
    try:
        return max(1, int(user.get("account_id") or 1))
    except (TypeError, ValueError):
        return 1


def logout():
    client = _get_client()
    if client:
        try:
            client.auth.sign_out()
        except Exception:
            pass
    st.session_state.pop(_KEY_USER, None)
    st.session_state.pop(_KEY_SESSION, None)


def render_logout_button():
    user = get_current_user()
    if not user:
        return
    email = user.get("email", "")
    _, col_btn = st.columns([8, 2])
    with col_btn:
        st.markdown(
            f"<div style='text-align:right;font-size:0.78rem;color:#6b7280;"
            f"padding:4px 0 2px;'>{email}</div>",
            unsafe_allow_html=True,
        )
        if st.button("Esci", key="pp_logout_btn", use_container_width=True):
            logout()
            st.rerun()


def _go_public(view: str, plan: str | None = None) -> None:
    st.session_state[_KEY_PUBLIC_VIEW] = view if view in PUBLIC_VIEWS else "landing"
    if plan:
        st.session_state[_KEY_SELECTED_PLAN] = normalize_plan(plan)
    st.rerun()


def _selected_plan() -> str:
    return normalize_plan(st.session_state.get(_KEY_SELECTED_PLAN, "free"))


def require_auth() -> bool:
    if get_current_user():
        return True

    auth_mode = os.environ.get("PRICEPILOT_AUTH_MODE", "local").strip().lower()
    if auth_mode == "disabled":
        st.sidebar.warning(
            "Autenticazione disabilitata. Stai usando PRICEPILOT_AUTH_MODE=disabled.",
            icon="!",
        )
        return True

    client = _get_client()
    _render_auth_page(client)
    return False


def _render_auth_page(client):
    _inject_public_css()
    view = st.session_state.get(_KEY_PUBLIC_VIEW, "landing")
    if view not in PUBLIC_VIEWS:
        view = "landing"
    if view == "landing":
        _render_landing_page()
    else:
        _render_auth_panel(client, view)


def _inject_public_css():
    st.markdown("""
    <style>
    [data-testid="stSidebar"], [data-testid="collapsedControl"] { display:none !important; }
    #MainMenu, footer { visibility:hidden; }
    .stApp { background:#f7f3ef; color:#151312; }
    .block-container { max-width:1180px; padding-top:1.2rem; padding-bottom:3rem; }
    .stButton > button[kind="primary"], .stButton > button[data-testid="baseButton-primary"] {
      background:#b33b2e; border-color:#b33b2e; color:white; font-weight:850;
      border-radius:10px; box-shadow:0 10px 24px rgba(179,59,46,.22);
    }
    .stButton > button[kind="secondary"], .stButton > button[data-testid="baseButton-secondary"] {
      border-radius:10px; border-color:#d9d0c8; color:#1f1b18; font-weight:750;
      background:#fffaf6;
    }
    .pp-nav { display:flex; align-items:center; justify-content:space-between;
              border:1px solid #e3d9d0; border-radius:18px; padding:12px 16px;
              background:rgba(255,250,246,.94); position:sticky; top:12px; z-index:10;
              box-shadow:0 14px 36px rgba(35,25,20,.08); backdrop-filter:blur(10px); }
    .pp-logo { display:flex; align-items:center; gap:10px; font-weight:950; color:#171312; font-size:1.08rem; }
    .pp-logo-mark { width:31px; height:31px; border-radius:10px; background:#171312;
                    color:white; display:inline-flex; align-items:center; justify-content:center; font-weight:900; }
    .pp-nav-links { display:flex; align-items:center; justify-content:center; gap:22px; font-size:.88rem; }
    .pp-nav-links a { color:#5f5650; text-decoration:none; font-weight:750; }
    .pp-nav-links a:hover { color:#b33b2e; }
    .pp-eyebrow { display:inline-flex; align-items:center; gap:8px; color:#8f2f25; background:#fff3ee;
                  border:1px solid #f0c4b8; border-radius:999px; padding:7px 12px;
                  font-size:.78rem; font-weight:800; letter-spacing:.03em; }
    .pp-hero { padding:38px 0 16px; }
    .pp-hero h1 { font-size:3rem; line-height:1.03; letter-spacing:0; color:#171312; margin:16px 0 14px; font-weight:950; }
    .pp-hero p { font-size:1.08rem; line-height:1.75; color:#5c514b; max-width:660px; margin-bottom:26px; }
    .pp-hero-metrics { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-top:26px; max-width:680px; }
    .pp-hero-metric { background:#fffaf6; border:1px solid #e5d9cf; border-radius:14px; padding:14px 15px;
      box-shadow:0 12px 30px rgba(35,25,20,.06); }
    .pp-hero-metric b { display:block; color:#171312; font-size:1.22rem; font-weight:950; }
    .pp-hero-metric span { color:#70645d; font-size:.78rem; line-height:1.35; }
    .pp-proof-row { display:flex; gap:10px; flex-wrap:wrap; margin-top:22px; color:#4f4742; font-size:.86rem; }
    .pp-proof { border:1px solid #e4d7cd; border-radius:999px; padding:8px 12px; background:#fffaf6; font-weight:750; }
    .pp-mockup { background:#191513; border-radius:26px; padding:15px; color:white;
      box-shadow:0 30px 80px rgba(30,20,16,.28); border:1px solid rgba(255,255,255,.10); }
    .pp-mock-top { display:flex; justify-content:space-between; align-items:center; padding:4px 4px 14px; }
    .pp-dot-row span { display:inline-block; width:9px; height:9px; border-radius:99px; margin-right:5px; background:#6f625b; }
    .pp-mock-status { color:#eadbd1; font-size:.76rem; font-weight:850; background:rgba(179,59,46,.18);
      border:1px solid rgba(255,255,255,.09); border-radius:999px; padding:5px 9px; }
    .pp-mock-grid { display:grid; grid-template-columns:1.15fr .85fr; gap:12px; }
    .pp-panel { background:#231d1a; border:1px solid rgba(255,255,255,.08); border-radius:16px; padding:12px; }
    .pp-panel.light { background:#fff8f2; color:#171312; border-color:#e7d7cc; }
    .pp-panel-title { font-size:.7rem; color:#cdbdb3; text-transform:uppercase; font-weight:900; letter-spacing:.08em; margin-bottom:10px; }
    .pp-panel.light .pp-panel-title { color:#8d7b70; }
    .pp-calendar { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; }
    .pp-day { background:#302924; border:1px solid rgba(255,255,255,.06); border-radius:11px; padding:7px 7px; min-height:46px; }
    .pp-day strong { display:block; color:#fffaf6; font-size:.94rem; }
    .pp-day span { color:#beafa5; font-size:.72rem; }
    .pp-day.hot { background:#8f2f25; }
    .pp-day.warn { background:#b76b2f; }
    .pp-day.locked { background:#51443c; }
    .pp-mini-kpi { background:#fffaf6; color:#171312; border:1px solid #eadbd1; border-radius:14px; padding:8px 10px; margin-bottom:6px; }
    .pp-mini-kpi b { display:block; font-size:1.04rem; font-weight:950; }
    .pp-mini-kpi span { color:#6d625c; font-size:.76rem; }
    .pp-bars { display:flex; align-items:end; gap:8px; height:46px; padding-top:6px; }
    .pp-bar { flex:1; border-radius:9px 9px 4px 4px; background:#b33b2e; min-height:26px; }
    .pp-bar:nth-child(2n) { background:#d9a271; }
    .pp-bar:nth-child(3n) { background:#fff0e5; }
    .pp-telegram { margin-top:0; background:#0f1419; border:1px solid rgba(255,255,255,.10); border-radius:16px;
      padding:10px; box-shadow:0 14px 34px rgba(0,0,0,.22); }
    .pp-telegram-head { display:flex; justify-content:space-between; color:#e8d9ce; font-size:.78rem; font-weight:850; margin-bottom:8px; }
    .pp-telegram-price { color:white; font-size:1rem; font-weight:900; margin:4px 0 10px; }
    .pp-telegram-actions { display:flex; gap:8px; }
    .pp-tg-approve, .pp-tg-reject { flex:1; border-radius:10px; padding:8px; text-align:center; font-size:.78rem; font-weight:900; }
    .pp-tg-approve { background:#b33b2e; color:white; }
    .pp-tg-reject { background:#2c2521; color:#eadbd1; border:1px solid rgba(255,255,255,.10); }
    .pp-section { padding:58px 0; }
    .pp-section h2 { color:#171312; font-size:2.22rem; font-weight:950; letter-spacing:0; margin-bottom:10px; }
    .pp-section-lead { color:#625852; font-size:1rem; max-width:760px; line-height:1.75; margin-bottom:28px; }
    .pp-dark { background:#171312; color:#fffaf6; border-radius:28px; padding:54px 44px; margin:46px 0;
      box-shadow:0 28px 70px rgba(30,20,16,.20); }
    .pp-dark h2 { color:#fffaf6; font-size:2.24rem; margin:0 0 12px; font-weight:950; }
    .pp-dark p { color:#d8c7bc; line-height:1.7; }
    .pp-dark-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; margin-top:24px; }
    .pp-card { height:100%; background:#fffaf6; border:1px solid #e6d9cf; border-radius:14px;
      padding:22px; box-shadow:0 14px 34px rgba(35,25,20,.055); transition:all .18s ease; }
    .pp-card:hover { transform:translateY(-3px); border-color:#d6afa4; box-shadow:0 22px 46px rgba(35,25,20,.10); }
    .pp-dark .pp-card { background:#211b18; border-color:rgba(255,255,255,.10); box-shadow:none; }
    .pp-dark .pp-card h3 { color:#fffaf6; }
    .pp-dark .pp-card p { color:#d8c7bc; }
    .pp-icon { width:42px; height:42px; border-radius:12px; display:flex; align-items:center; justify-content:center;
      background:#f8e2da; color:#8f2f25; font-weight:900; margin-bottom:14px; border:1px solid #efc8bc; font-size:1.15rem; }
    .pp-card h3 { font-size:1.03rem; color:#171312; font-weight:900; margin:0 0 8px; }
    .pp-card p { color:#625852; font-size:.92rem; line-height:1.62; margin:0; }
    .pp-step { background:#fffaf6; border:1px solid #e6d9cf; border-radius:16px; padding:22px; height:100%;
      box-shadow:0 14px 34px rgba(35,25,20,.055); }
    .pp-step-num { width:34px; height:34px; border-radius:999px; background:#171312; color:white;
      display:flex; align-items:center; justify-content:center; font-weight:950; margin-bottom:14px; }
    .pp-step-preview { margin-top:16px; background:#f4ebe3; border:1px solid #e7d6ca; border-radius:13px; padding:12px; color:#554a44; font-size:.8rem; }
    .pp-preview-row { display:flex; justify-content:space-between; gap:10px; padding:6px 0; border-bottom:1px solid #e4d4c9; }
    .pp-preview-row:last-child { border-bottom:0; }
    .pp-price { position:relative; height:100%; background:#fffaf6; border:1px solid #e6d9cf; border-radius:16px;
      padding:25px; box-shadow:0 16px 38px rgba(35,25,20,.06); }
    .pp-price.recommended { border:2px solid #b33b2e; box-shadow:0 24px 58px rgba(179,59,46,.16); }
    .pp-badge { position:absolute; top:15px; right:15px; background:#b33b2e; color:white; border-radius:999px;
      padding:6px 11px; font-size:.72rem; font-weight:900; }
    .pp-price-name { font-size:1.18rem; font-weight:950; color:#171312; margin-bottom:6px; padding-right:86px; }
    .pp-price-sub { color:#8f2f25; font-size:.82rem; font-weight:900; text-transform:uppercase; letter-spacing:.04em; }
    .pp-price-value { font-size:2.15rem; font-weight:950; color:#171312; margin:13px 0 8px; }
    .pp-price-desc { color:#625852; min-height:84px; font-size:.9rem; line-height:1.58; }
    .pp-plan-mode { margin:14px 0; border-radius:12px; padding:10px 12px; background:#f4ebe3; color:#4f4540;
      font-weight:850; font-size:.84rem; }
    .pp-feature-list, .pp-missing-list { margin:16px 0 0; padding:0; list-style:none; color:#3e3733; font-size:.86rem; line-height:1.95; }
    .pp-feature-list li:before { content:"✓"; color:#8f2f25; font-weight:950; margin-right:8px; }
    .pp-missing-list { color:#7f7067; border-top:1px solid #eadbd1; padding-top:12px; }
    .pp-missing-list li:before { content:"–"; color:#9b8b82; font-weight:950; margin-right:8px; }
    .pp-final-cta { background:#171312; color:#fffaf6; border-radius:28px; padding:54px 44px; margin:54px 0 28px;
      text-align:center; box-shadow:0 28px 70px rgba(30,20,16,.22); }
    .pp-final-cta h2 { color:#fffaf6; font-size:2.35rem; font-weight:950; margin:0 0 12px; }
    .pp-final-cta p { color:#d8c7bc; margin:0 auto 22px; max-width:680px; line-height:1.7; }
    .pp-auth-wrap { min-height:88vh; display:flex; align-items:center; justify-content:center; padding:34px 0; }
    .pp-auth-card { width:100%; max-width:520px; background:#fffaf6; border:1px solid #e6d9cf; border-radius:18px;
                    padding:28px; box-shadow:0 22px 70px rgba(35,25,20,.12); }
    .pp-auth-title { color:#171312; font-size:1.55rem; font-weight:950; margin:8px 0; }
    .pp-auth-copy { color:#625852; font-size:.92rem; line-height:1.6; margin-bottom:18px; }
    .pp-plan-pill { display:inline-flex; align-items:center; gap:8px; background:#fff3ee; color:#8f2f25;
                    border:1px solid #f0c4b8; border-radius:999px; padding:6px 10px; font-size:.78rem; font-weight:850; }
    @media (max-width: 760px) {
      .pp-nav { position:relative; top:auto; align-items:flex-start; gap:12px; }
      .pp-nav-links { display:none; }
      .pp-hero { padding:38px 0 26px; }
      .pp-hero h1 { font-size:2.5rem; }
      .pp-mock-grid, .pp-hero-metrics { grid-template-columns:1fr; }
      .pp-dark-grid { grid-template-columns:1fr; }
      .pp-dark, .pp-final-cta { padding:34px 22px; border-radius:20px; }
      .pp-section h2, .pp-dark h2, .pp-final-cta h2 { font-size:1.75rem; }
    }
    </style>
    """, unsafe_allow_html=True)
    st.markdown("""
    <style>
    html { scroll-behavior:smooth; }
    .stApp { background:#ffffff; color:#000000; }
    header, [data-testid="stToolbar"], [data-testid="stDecoration"], [data-testid="stStatusWidget"],
    [data-testid="stDeployButton"], .stDeployButton { display:none !important; visibility:hidden !important; }
    .block-container { max-width:1200px; padding-top:1rem; padding-bottom:4rem; }
    .stButton > button[kind="primary"], .stButton > button[data-testid="baseButton-primary"] {
      background:#B5523A !important; border-color:#B5523A !important; color:#ffffff !important;
      border-radius:8px !important; box-shadow:none !important; font-weight:800 !important;
      min-height:42px; transition:transform .16s ease, background .16s ease, border-color .16s ease;
    }
    .stButton > button[kind="primary"]:hover, .stButton > button[data-testid="baseButton-primary"]:hover {
      transform:translateY(-1px); background:#000000 !important; border-color:#000000 !important;
    }
    .stButton > button[kind="secondary"], .stButton > button[data-testid="baseButton-secondary"] {
      background:#ffffff !important; color:#000000 !important; border:1px solid rgba(0,0,0,.16) !important;
      border-radius:8px !important; box-shadow:none !important; font-weight:750 !important; min-height:42px;
      transition:transform .16s ease, border-color .16s ease;
    }
    .stButton > button[kind="secondary"]:hover, .stButton > button[data-testid="baseButton-secondary"]:hover {
      transform:translateY(-1px); border-color:#000000 !important;
    }
    .pp-nav { border:1px solid rgba(0,0,0,.08) !important; border-radius:14px !important;
      background:rgba(255,255,255,.88) !important; box-shadow:0 16px 44px rgba(0,0,0,.06) !important;
      backdrop-filter:blur(14px); padding:10px 14px !important; }
    .pp-logo { color:#000000 !important; font-weight:900 !important; letter-spacing:-.02em; }
    .pp-logo-mark { background:#000000 !important; color:#ffffff !important; border-radius:8px !important; }
    .pp-nav-links a { color:rgba(0,0,0,.68) !important; font-weight:760 !important; }
    .pp-nav-links a:hover { color:#B5523A !important; }

    .pp-enterprise-hero { padding:86px 0 86px; }
    .pp-eyebrow { background:#ffffff !important; color:#B5523A !important; border:1px solid rgba(181,82,58,.26) !important;
      border-radius:999px; padding:7px 12px; font-size:.76rem; font-weight:900; letter-spacing:.04em; }
    .pp-enterprise-hero h1 { color:#000000; font-size:4.6rem; line-height:.96; letter-spacing:-.055em;
      margin:22px 0 20px; font-weight:950; max-width:720px; }
    .pp-enterprise-hero p { color:rgba(0,0,0,.62); font-size:1.08rem; line-height:1.75; max-width:650px; margin:0 0 30px; }
    .pp-proof-row { display:flex; gap:10px; flex-wrap:wrap; margin-top:22px; }
    .pp-proof { color:rgba(0,0,0,.70) !important; background:#ffffff !important; border:1px solid rgba(0,0,0,.10) !important;
      border-radius:999px; padding:8px 12px; font-size:.84rem; font-weight:760; }
    .pp-proof strong { color:#000000; }
    .pp-metric-strip { display:grid; grid-template-columns:repeat(3,1fr); border:1px solid rgba(0,0,0,.10);
      border-radius:16px; margin:32px 0 0; overflow:hidden; max-width:700px; }
    .pp-metric-strip div { padding:18px 20px; border-right:1px solid rgba(0,0,0,.10); }
    .pp-metric-strip div:last-child { border-right:0; }
    .pp-metric-strip b { display:block; color:#000000; font-size:1.35rem; letter-spacing:-.03em; }
    .pp-metric-strip span { display:block; color:rgba(0,0,0,.55); font-size:.82rem; margin-top:4px; }

    .pp-product-shell { background:#000000 !important; color:#ffffff; border-radius:22px; padding:16px;
      border:1px solid rgba(0,0,0,.92); box-shadow:0 28px 70px rgba(0,0,0,.20); }
    .pp-product-top { display:flex; justify-content:space-between; align-items:center; padding:2px 2px 14px; }
    .pp-window-dots span { display:inline-block; width:8px; height:8px; border-radius:99px; background:rgba(255,255,255,.36); margin-right:5px; }
    .pp-product-pill { border:1px solid rgba(255,255,255,.20); border-radius:999px; color:rgba(255,255,255,.76);
      font-size:.72rem; padding:5px 9px; font-weight:800; }
    .pp-product-grid { display:grid; grid-template-columns:1.2fr .8fr; gap:10px; }
    .pp-shot-panel { background:#ffffff; color:#000000; border-radius:14px; border:1px solid rgba(255,255,255,.12); padding:12px; }
    .pp-shot-panel.dark { background:#0a0a0a; color:#ffffff; border:1px solid rgba(255,255,255,.12); }
    .pp-shot-label { color:rgba(0,0,0,.48); font-size:.68rem; letter-spacing:.08em; text-transform:uppercase; font-weight:900; margin-bottom:10px; }
    .pp-shot-panel.dark .pp-shot-label { color:rgba(255,255,255,.52); }
    .pp-calendar-clean { display:grid; grid-template-columns:repeat(4,1fr); gap:7px; }
    .pp-date-clean { border:1px solid rgba(0,0,0,.12); border-radius:10px; padding:8px 7px; min-height:54px; background:#ffffff; }
    .pp-date-clean b { display:block; font-size:.94rem; letter-spacing:-.02em; }
    .pp-date-clean span { display:block; color:rgba(0,0,0,.52); font-size:.68rem; margin-top:3px; }
    .pp-date-clean.hot { background:#B5523A; color:#ffffff; border-color:#B5523A; }
    .pp-date-clean.hot span { color:rgba(255,255,255,.78); }
    .pp-date-clean.locked { background:#000000; color:#ffffff; border-color:#000000; }
    .pp-date-clean.locked span { color:rgba(255,255,255,.72); }
    .pp-kpi-clean { display:grid; gap:8px; }
    .pp-kpi-clean div { border:1px solid rgba(0,0,0,.12); border-radius:12px; padding:10px; background:#ffffff; }
    .pp-kpi-clean span { display:block; color:rgba(0,0,0,.52); font-size:.74rem; }
    .pp-kpi-clean b { display:block; color:#000000; font-size:1.22rem; margin-top:4px; }
    .pp-bars-clean { display:flex; gap:8px; align-items:end; height:92px; }
    .pp-bars-clean i { display:block; flex:1; background:#000000; border-radius:7px 7px 2px 2px; min-height:18px; }
    .pp-bars-clean i.brick { background:#B5523A; }
    .pp-table-clean { display:grid; gap:8px; }
    .pp-row-clean { display:grid; grid-template-columns:1fr auto auto; gap:8px; align-items:center;
      color:rgba(255,255,255,.80); border-bottom:1px solid rgba(255,255,255,.10); padding-bottom:7px; font-size:.76rem; }
    .pp-row-clean b { color:#ffffff; }
    .pp-telegram-clean { background:#000000; color:#ffffff; border:1px solid rgba(255,255,255,.16);
      border-radius:14px; padding:12px; }
    .pp-telegram-clean h4 { margin:0 0 8px; font-size:.86rem; color:#ffffff; }
    .pp-telegram-clean p { margin:0 0 10px; color:rgba(255,255,255,.68); font-size:.76rem; line-height:1.45; }
    .pp-tg-actions { display:flex; gap:8px; }
    .pp-tg-actions span { flex:1; border-radius:8px; text-align:center; padding:7px 8px; font-size:.74rem; font-weight:900; }
    .pp-tg-actions span:first-child { background:#B5523A; color:#ffffff; }
    .pp-tg-actions span:last-child { border:1px solid rgba(255,255,255,.18); color:#ffffff; }

    .pp-section { padding:92px 0 !important; }
    .pp-section h2, .pp-feature-copy h2 { font-size:3rem; line-height:1.04; letter-spacing:-.045em; color:#000000; font-weight:950; margin:0 0 16px; }
    .pp-section-lead, .pp-feature-copy p { color:rgba(0,0,0,.58); font-size:1.03rem; line-height:1.75; max-width:680px; }
    .pp-feature-row { display:grid; grid-template-columns:1fr 1fr; gap:78px; align-items:center; padding:96px 0; border-top:1px solid rgba(0,0,0,.08); }
    .pp-feature-row.reverse .pp-feature-copy { order:2; }
    .pp-feature-row.reverse .pp-mini-shot { order:1; }
    .pp-feature-kicker { color:#B5523A; text-transform:uppercase; letter-spacing:.08em; font-weight:900; font-size:.74rem; margin-bottom:14px; }
    .pp-feature-points { margin-top:24px; display:grid; gap:11px; }
    .pp-feature-points span { color:rgba(0,0,0,.68); font-size:.92rem; }
    .pp-feature-points span:before { content:""; display:inline-block; width:7px; height:7px; border-radius:99px; background:#B5523A; margin-right:10px; }
    .pp-mini-shot { border:1px solid rgba(0,0,0,.10); border-radius:20px; background:#ffffff; padding:16px;
      box-shadow:0 24px 70px rgba(0,0,0,.08); transition:transform .18s ease, box-shadow .18s ease; }
    .pp-mini-shot:hover { transform:translateY(-3px); box-shadow:0 32px 82px rgba(0,0,0,.10); }
    .pp-mini-shot.dark { background:#000000; color:#ffffff; border-color:#000000; }
    .pp-mini-shot-grid { display:grid; grid-template-columns:repeat(2,1fr); gap:10px; }
    .pp-mini-card { border:1px solid rgba(0,0,0,.12); border-radius:13px; padding:13px; min-height:86px; }
    .pp-mini-shot.dark .pp-mini-card { border-color:rgba(255,255,255,.16); }
    .pp-mini-card small { color:rgba(0,0,0,.52); font-weight:800; }
    .pp-mini-shot.dark .pp-mini-card small { color:rgba(255,255,255,.56); }
    .pp-mini-card b { display:block; margin-top:8px; font-size:1.25rem; letter-spacing:-.03em; }

    .pp-timeline { display:grid; grid-template-columns:repeat(3,1fr); gap:0; border:1px solid rgba(0,0,0,.10);
      border-radius:18px; overflow:hidden; background:#ffffff; }
    .pp-time-step { padding:28px; border-right:1px solid rgba(0,0,0,.10); }
    .pp-time-step:last-child { border-right:0; }
    .pp-time-step strong { display:inline-flex; align-items:center; justify-content:center; width:30px; height:30px;
      border-radius:99px; background:#000000; color:#ffffff; margin-bottom:28px; }
    .pp-time-step h3 { margin:0 0 10px; font-size:1.15rem; color:#000000; letter-spacing:-.02em; }
    .pp-time-step p { margin:0; color:rgba(0,0,0,.58); line-height:1.6; font-size:.92rem; }

    .pp-price { background:#ffffff !important; border:1px solid rgba(0,0,0,.10) !important; border-radius:18px !important;
      box-shadow:none !important; padding:28px !important; transition:transform .18s ease, border-color .18s ease, box-shadow .18s ease; }
    .pp-price:hover { transform:translateY(-3px); box-shadow:0 24px 64px rgba(0,0,0,.08) !important; }
    .pp-price.recommended { border:2px solid #B5523A !important; box-shadow:0 24px 70px rgba(181,82,58,.11) !important; }
    .pp-badge { background:#B5523A !important; color:#ffffff !important; border-radius:999px !important; }
    .pp-price-name, .pp-price-value { color:#000000 !important; }
    .pp-price-sub { color:#B5523A !important; }
    .pp-price-desc, .pp-feature-list, .pp-missing-list { color:rgba(0,0,0,.62) !important; }
    .pp-plan-mode { background:#000000 !important; color:#ffffff !important; border-radius:10px !important; }
    .pp-feature-list li:before { content:""; display:inline-block; width:7px; height:7px; border-radius:99px; background:#B5523A; margin-right:10px; }
    .pp-missing-list { border-top:1px solid rgba(0,0,0,.10) !important; }
    .pp-missing-list li:before { content:""; display:inline-block; width:7px; height:1px; background:rgba(0,0,0,.42); margin-right:10px; vertical-align:middle; }

    .pp-faq-wrap [data-testid="stExpander"] { border:1px solid rgba(0,0,0,.10); border-radius:14px; box-shadow:none; background:#ffffff; }
    .pp-final-cta { background:#000000 !important; color:#ffffff !important; border-radius:24px !important; padding:76px 44px !important;
      box-shadow:none !important; margin:80px 0 22px !important; }
    .pp-final-cta h2 { color:#ffffff !important; font-size:3.2rem !important; letter-spacing:-.045em; }
    .pp-final-cta p { color:rgba(255,255,255,.66) !important; }

    @media (max-width: 900px) {
      .pp-enterprise-hero { padding:54px 0; }
      .pp-enterprise-hero h1 { font-size:3rem; }
      .pp-product-grid, .pp-feature-row, .pp-feature-row.reverse { grid-template-columns:1fr; gap:28px; }
      .pp-feature-row.reverse .pp-feature-copy, .pp-feature-row.reverse .pp-mini-shot { order:initial; }
      .pp-timeline, .pp-metric-strip { grid-template-columns:1fr; }
      .pp-metric-strip div, .pp-time-step { border-right:0; border-bottom:1px solid rgba(0,0,0,.10); }
      .pp-metric-strip div:last-child, .pp-time-step:last-child { border-bottom:0; }
      .pp-section h2, .pp-feature-copy h2, .pp-final-cta h2 { font-size:2.05rem !important; }
    }

    @keyframes ppFadeUp {
      from { opacity:0; transform:translateY(12px); }
      to { opacity:1; transform:translateY(0); }
    }
    @keyframes ppFloatSoft {
      0%, 100% { transform:translateY(0); }
      50% { transform:translateY(-4px); }
    }
    .pp-enterprise-hero { padding:60px 0 46px !important; }
    .pp-enterprise-hero h1 { margin-top:18px !important; margin-bottom:18px !important; }
    .pp-metric-strip { margin-top:24px !important; }
    .pp-section { padding:66px 0 !important; }
    .pp-feature-row { padding:68px 0 !important; gap:56px !important; }
    .pp-final-cta { margin:58px 0 22px !important; padding:66px 44px !important; }
    .pp-product-shell { animation:ppFadeUp .6s ease both, ppFloatSoft 8s ease-in-out infinite; }
    .pp-product-shell:hover { animation-play-state:paused; }
    .pp-mini-shot, .pp-price, .pp-proof-panel { animation:ppFadeUp .55s ease both; }

    .pp-proof-panel { margin:2px 0 48px; padding:18px; border:1px solid rgba(0,0,0,.10);
      border-radius:22px; background:#ffffff; box-shadow:0 24px 70px rgba(0,0,0,.055);
      transition:transform .18s ease, box-shadow .18s ease, border-color .18s ease; }
    .pp-proof-panel:hover { transform:translateY(-2px); border-color:rgba(181,82,58,.28);
      box-shadow:0 30px 80px rgba(0,0,0,.07); }
    .pp-proof-top { display:grid; grid-template-columns:1.05fr .95fr; gap:22px; align-items:center; padding:4px 4px 16px; }
    .pp-proof-kicker { color:#B5523A; text-transform:uppercase; letter-spacing:.08em; font-weight:900; font-size:.72rem; margin-bottom:8px; }
    .pp-proof-title { margin:0; color:#000000; font-size:1.55rem; line-height:1.12; letter-spacing:-.035em; font-weight:950; }
    .pp-live-status { display:flex; justify-content:flex-end; gap:8px; flex-wrap:wrap; }
    .pp-status-chip { display:inline-flex; align-items:center; gap:8px; border:1px solid rgba(0,0,0,.10);
      border-radius:999px; padding:8px 10px; color:rgba(0,0,0,.70); font-size:.78rem; font-weight:800; background:#ffffff; }
    .pp-status-dot { width:7px; height:7px; border-radius:99px; background:#B5523A; box-shadow:0 0 0 4px rgba(181,82,58,.10); }
    .pp-logo-wall { display:grid; grid-template-columns:repeat(8,minmax(0,1fr)); gap:1px;
      background:rgba(0,0,0,.10); border:1px solid rgba(0,0,0,.10); border-radius:16px; overflow:hidden; }
    .pp-logo-wall span { background:#ffffff; color:rgba(0,0,0,.54); text-align:center;
      padding:14px 8px; font-size:.82rem; font-weight:900; letter-spacing:-.01em; }
    .pp-proof-metrics { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin-top:12px; }
    .pp-proof-metrics div { border:1px solid rgba(0,0,0,.10); border-radius:14px; padding:14px; background:#ffffff; }
    .pp-proof-metrics b { display:block; color:#000000; font-size:1.32rem; letter-spacing:-.035em; }
    .pp-proof-metrics span { display:block; color:rgba(0,0,0,.58); font-size:.78rem; line-height:1.45; margin-top:5px; }

    .pp-ops-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:10px; }
    .pp-sync-row { display:flex; align-items:center; justify-content:space-between; gap:10px;
      border-bottom:1px solid rgba(0,0,0,.10); padding:8px 0; font-size:.76rem; color:rgba(0,0,0,.70); }
    .pp-sync-row:last-child { border-bottom:0; }
    .pp-sync-row b { color:#000000; }
    .pp-sync-row.dark { border-color:rgba(255,255,255,.12); color:rgba(255,255,255,.72); }
    .pp-sync-row.dark b { color:#ffffff; }
    .pp-mini-status { display:inline-flex; align-items:center; gap:6px; font-weight:900; color:#B5523A; }
    .pp-mini-status:before { content:""; width:7px; height:7px; border-radius:99px; background:#B5523A; }
    .pp-history-line { display:flex; align-items:flex-end; gap:6px; height:56px; margin-top:10px; }
    .pp-history-line i { flex:1; display:block; min-height:12px; border-radius:8px 8px 2px 2px; background:#000000; opacity:.86; }
    .pp-history-line i.brick { background:#B5523A; opacity:1; }
    .pp-log-item { border-bottom:1px solid rgba(255,255,255,.12); padding:8px 0; color:rgba(255,255,255,.72); font-size:.76rem; line-height:1.35; }
    .pp-log-item:last-child { border-bottom:0; }
    .pp-log-item b { display:block; color:#ffffff; font-size:.78rem; margin-bottom:2px; }

    .pp-auth-wrap { min-height:calc(100vh - 128px) !important; padding:16px 0 24px !important;
      display:flex !important; align-items:center !important; justify-content:center !important; }
    .pp-auth-card { width:100% !important; max-width:500px !important; margin:0 auto !important;
      background:#ffffff !important; border:1px solid rgba(0,0,0,.10) !important; border-radius:20px !important;
      padding:28px !important; box-shadow:0 24px 70px rgba(0,0,0,.08) !important; }
    .pp-auth-title { color:#000000 !important; font-size:1.55rem !important; letter-spacing:-.025em !important; }
    .pp-auth-copy { color:rgba(0,0,0,.58) !important; }
    .pp-plan-pill { background:#ffffff !important; color:#B5523A !important; border:1px solid rgba(181,82,58,.24) !important; }
    .pp-auth-top-gap { height:0; }
    div[data-testid="stVerticalBlockBorderWrapper"] { border:1px solid rgba(0,0,0,.10) !important;
      border-radius:20px !important; background:#ffffff !important; box-shadow:0 24px 70px rgba(0,0,0,.08) !important; }
    div[data-testid="stVerticalBlockBorderWrapper"] > div { border-radius:20px !important; }

    @media (max-width: 900px) {
      .pp-enterprise-hero { padding:44px 0 28px !important; }
      .pp-enterprise-hero h1 { font-size:3rem !important; line-height:1 !important; }
      .pp-proof-top, .pp-proof-metrics, .pp-ops-grid { grid-template-columns:1fr; }
      .pp-live-status { justify-content:flex-start; }
      .pp-logo-wall { grid-template-columns:repeat(2,minmax(0,1fr)); }
      .pp-section { padding:52px 0 !important; }
      .pp-feature-row { padding:54px 0 !important; }
      .block-container { padding-top:.7rem !important; }
      .pp-auth-top-gap { height:0; }
      .pp-auth-wrap { min-height:auto !important; padding:8px 0 20px !important; align-items:flex-start !important; }
      .pp-auth-card { max-width:100% !important; padding:22px 18px !important; border-radius:16px !important; box-shadow:0 16px 48px rgba(0,0,0,.07) !important; }
      div[data-testid="stVerticalBlockBorderWrapper"] { border-radius:16px !important; box-shadow:0 16px 48px rgba(0,0,0,.07) !important; }
    }
    </style>
    """, unsafe_allow_html=True)


def _render_public_nav():
    logo_col, links_col, login_col, cta_col = st.columns([2.0, 4.2, 1.0, 1.35])
    with logo_col:
        st.markdown(
            '<div class="pp-logo"><span class="pp-logo-mark">P</span><span>PricePilot</span></div>',
            unsafe_allow_html=True,
        )
    with links_col:
        st.markdown(
            '<div class="pp-nav-links">'
            '<a href="#funzionalita">Funzionalita</a>'
            '<a href="#come-funziona">Come funziona</a>'
            '<a href="#prezzi">Prezzi</a>'
            '<a href="#faq">FAQ</a>'
            '</div>',
            unsafe_allow_html=True,
        )
    with login_col:
        if st.button("Login", key="public_nav_login", use_container_width=True):
            _go_public("login")
    with cta_col:
        if st.button("Inizia Gratis", key="public_nav_signup", use_container_width=True, type="primary"):
            _go_public("register", "free")


def _render_landing_page():
    _render_public_nav()

    hero_left, hero_right = st.columns([1.05, 0.95], gap="large")
    with hero_left:
        st.markdown(
            '<section class="pp-enterprise-hero">'
            '<span class="pp-eyebrow">Revenue management per affitti brevi</span>'
            '<h1>Pricing dinamico che lavora come un revenue manager.</h1>'
            '<p>PricePilot monitora competitor, occupazione, stagionalita ed eventi per trasformare '
            'ogni variazione di mercato in una decisione prezzo chiara, approvabile o automatica.</p>'
            '</section>',
            unsafe_allow_html=True,
        )
        if st.button("Inizia Gratis", key="hero_start_free", use_container_width=True, type="primary"):
            _go_public("register", "free")
        st.markdown(
            '<div class="pp-proof-row">'
            '<span class="pp-proof"><strong>6h</strong> market refresh</span>'
            '<span class="pp-proof"><strong>Plus</strong> Telegram approval</span>'
            '<span class="pp-proof"><strong>Pro</strong> autopilot OTA</span>'
            '<span class="pp-proof">Multi property ready</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="pp-metric-strip">'
            '<div><b>14</b><span>competitor confrontati</span></div>'
            '<div><b>+19%</b><span>opportunita su date ad alta domanda</span></div>'
            '<div><b>1 click</b><span>approvazione cambio prezzo</span></div>'
            '</div>',
            unsafe_allow_html=True,
        )
    with hero_right:
        _render_dashboard_mockup()

    _render_social_proof_section()
    _render_features_section()
    _render_how_it_works_section()
    _render_pricing_section()
    _render_faq_section()
    _render_final_cta_section()


def _render_dashboard_mockup():
    st.markdown("""
    <div class="pp-product-shell">
      <div class="pp-product-top">
        <div class="pp-window-dots"><span></span><span></span><span></span></div>
        <div class="pp-product-pill">Villa Centro · live pricing</div>
      </div>
      <div class="pp-product-grid">
        <div class="pp-shot-panel">
          <div class="pp-shot-label">Calendario prezzi</div>
          <div class="pp-calendar-clean">
            <div class="pp-date-clean"><b>&euro;128</b><span>Lun</span></div>
            <div class="pp-date-clean"><b>&euro;134</b><span>Mar</span></div>
            <div class="pp-date-clean hot"><b>&euro;159</b><span>Ven</span></div>
            <div class="pp-date-clean hot"><b>&euro;172</b><span>Evento</span></div>
            <div class="pp-date-clean"><b>&euro;121</b><span>Dom</span></div>
            <div class="pp-date-clean locked"><b>&euro;200</b><span>Bloccato</span></div>
            <div class="pp-date-clean hot"><b>&euro;151</b><span>Weekend</span></div>
            <div class="pp-date-clean"><b>&euro;130</b><span>Gio</span></div>
          </div>
        </div>
        <div class="pp-kpi-clean">
          <div><span>Prezzo suggerito</span><b>&euro;172</b></div>
          <div><span>Occupazione stimata</span><b>81%</b></div>
          <div><span>Media competitor</span><b>&euro;154</b></div>
          <div><span>Prossima analisi</span><b>6h</b></div>
        </div>
      </div>
      <div class="pp-product-grid" style="margin-top:10px">
        <div class="pp-shot-panel dark">
          <div class="pp-shot-label">Revenue forecast</div>
          <div class="pp-bars-clean">
            <i style="height:38%"></i>
            <i class="brick" style="height:54%"></i>
            <i style="height:46%"></i>
            <i class="brick" style="height:74%"></i>
            <i style="height:62%"></i>
            <i class="brick" style="height:92%"></i>
          </div>
          <div class="pp-table-clean" style="margin-top:16px">
            <div class="pp-row-clean"><span>Competitor A</span><b>&euro;168</b><span>+9%</span></div>
            <div class="pp-row-clean"><span>Competitor B</span><b>&euro;151</b><span>-2%</span></div>
            <div class="pp-row-clean"><span>Competitor C</span><b>&euro;181</b><span>+17%</span></div>
          </div>
        </div>
        <div class="pp-telegram-clean">
          <h4>Telegram approval</h4>
          <p>Nuovo prezzo suggerito: <strong>&euro;145 &rarr; &euro;172</strong><br>Motivo: evento locale, weekend e competitor sopra media.</p>
          <div class="pp-tg-actions">
            <span>Approva</span><span>Rifiuta</span>
          </div>
        </div>
      </div>
      <div class="pp-ops-grid">
        <div class="pp-shot-panel">
          <div class="pp-shot-label">Market analytics & price history</div>
          <div class="pp-sync-row"><span>Market pulse</span><b>Alta domanda</b></div>
          <div class="pp-sync-row"><span>Confidence</span><b>92%</b></div>
          <div class="pp-history-line">
            <i style="height:28%"></i><i style="height:34%"></i><i class="brick" style="height:49%"></i>
            <i style="height:44%"></i><i class="brick" style="height:68%"></i><i class="brick" style="height:86%"></i>
            <i style="height:58%"></i>
          </div>
        </div>
        <div class="pp-shot-panel dark">
          <div class="pp-shot-label">OTA sync & automation log</div>
          <div class="pp-sync-row dark"><span>Airbnb</span><span class="pp-mini-status">Synced</span></div>
          <div class="pp-sync-row dark"><span>Booking.com</span><span class="pp-mini-status">Queued</span></div>
          <div class="pp-log-item"><b>09:00 pricing cycle</b>23 decisioni generate, 19 pronte per sync.</div>
          <div class="pp-log-item"><b>09:04 guardrail check</b>Nessun prezzo fuori range commerciale.</div>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)
    return
    st.markdown("""
    <div class="pp-mockup">
      <div class="pp-mock-top">
        <div class="pp-dot-row"><span></span><span></span><span></span></div>
        <div class="pp-mock-status">Mercato live · Lucca Centro</div>
      </div>
      <div class="pp-mock-grid">
        <div class="pp-panel">
          <div class="pp-panel-title">Calendario smart</div>
          <div class="pp-calendar">
            <div class="pp-day"><strong>&euro;128</strong><span>Lun · base</span></div>
            <div class="pp-day"><strong>&euro;134</strong><span>Mar · ok</span></div>
            <div class="pp-day hot"><strong>&euro;159</strong><span>Ven · domanda</span></div>
            <div class="pp-day warn"><strong>&euro;172</strong><span>Sab · evento</span></div>
            <div class="pp-day"><strong>&euro;121</strong><span>Dom · scarico</span></div>
            <div class="pp-day locked"><strong>&euro;200</strong><span>Override</span></div>
            <div class="pp-day hot"><strong>&euro;151</strong><span>Weekend</span></div>
            <div class="pp-day"><strong>&euro;130</strong><span>Gio · medio</span></div>
          </div>
        </div>
        <div>
          <div class="pp-mini-kpi"><span>Prezzo suggerito oggi</span><b>&euro;172</b></div>
          <div class="pp-mini-kpi"><span>Media mercato</span><b>&euro;154</b></div>
          <div class="pp-mini-kpi"><span>Prossima analisi</span><b>6h</b></div>
        </div>
      </div>
      <div class="pp-mock-grid" style="margin-top:12px">
        <div class="pp-panel">
          <div class="pp-panel-title">Mini grafico revenue</div>
          <div class="pp-bars">
            <div class="pp-bar" style="height:42%"></div>
            <div class="pp-bar" style="height:58%"></div>
            <div class="pp-bar" style="height:49%"></div>
            <div class="pp-bar" style="height:76%"></div>
            <div class="pp-bar" style="height:70%"></div>
            <div class="pp-bar" style="height:94%"></div>
          </div>
        </div>
        <div class="pp-telegram">
          <div class="pp-telegram-head"><span>Telegram approval</span><span>Plus</span></div>
          <div style="color:#beafa5;font-size:.78rem">Nuovo prezzo suggerito</div>
          <div class="pp-telegram-price">&euro;145 &rarr; &euro;172</div>
          <div style="color:#beafa5;font-size:.75rem;margin-bottom:10px">Motivo: evento + domanda weekend + competitor sopra media.</div>
          <div class="pp-telegram-actions">
            <div class="pp-tg-approve">Approva</div>
            <div class="pp-tg-reject">Rifiuta</div>
          </div>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)


def _render_social_proof_section():
    st.markdown("""
    <section class="pp-proof-panel">
      <div class="pp-proof-top">
        <div>
          <div class="pp-proof-kicker">Enterprise hospitality revenue platform</div>
          <h3 class="pp-proof-title">Pensato per host singoli, property manager e portfolio in crescita.</h3>
        </div>
        <div class="pp-live-status">
          <span class="pp-status-chip"><i class="pp-status-dot"></i>Live market monitor</span>
          <span class="pp-status-chip"><i class="pp-status-dot"></i>OTA-ready</span>
          <span class="pp-status-chip"><i class="pp-status-dot"></i>Guardrail attivi</span>
        </div>
      </div>
      <div class="pp-logo-wall">
        <span>Airbnb</span><span>Booking.com</span><span>Vrbo</span><span>Hostaway</span>
        <span>Guesty</span><span>Beds24</span><span>Smoobu</span><span>Lodgify</span>
      </div>
      <div class="pp-proof-metrics">
        <div><b>6h</b><span>ciclo analisi mercato configurabile</span></div>
        <div><b>+19%</b><span>opportunita media su date ad alta domanda</span></div>
        <div><b>1 click</b><span>approval Telegram per il piano Plus</span></div>
        <div><b>25</b><span>proprieta gestibili nel piano Pro</span></div>
      </div>
    </section>
    """, unsafe_allow_html=True)


def _render_problem_solution_section():
    st.markdown(
        '<section class="pp-dark">'
        '<h2>Cambiare i prezzi a mano ti fa perdere ricavi</h2>'
        '<p>Mercato, eventi, weekend e competitor cambiano continuamente. '
        'PricePilot monitora questi segnali e ti aiuta a prendere decisioni di prezzo '
        'piu veloci e motivate.</p>'
        '<div class="pp-dark-grid">'
        '<div class="pp-card"><div class="pp-icon">!</div><h3>Prima</h3>'
        '<p>Prezzi aggiornati manualmente, spesso troppo tardi.</p></div>'
        '<div class="pp-card"><div class="pp-icon">→</div><h3>Con PricePilot</h3>'
        '<p>Suggerimenti automatici basati su mercato e occupazione.</p></div>'
        '<div class="pp-card"><div class="pp-icon">✓</div><h3>Risultato</h3>'
        '<p>Piu controllo, meno tempo perso, piu opportunita di revenue.</p></div>'
        '</div>'
        '</section>',
        unsafe_allow_html=True,
    )


def _render_features_section():
    st.markdown('<span id="funzionalita"></span>', unsafe_allow_html=True)
    st.markdown(
        '<section class="pp-section" style="padding-bottom:24px !important">'
        '<h2>Un sistema operativo per il pricing.</h2>'
        '<p class="pp-section-lead">PricePilot combina dati di mercato, regole commerciali e approvazioni operative in un flusso pensato per host e property manager.</p>'
        '</section>',
        unsafe_allow_html=True,
    )
    st.markdown("""
    <section class="pp-feature-row">
      <div class="pp-feature-copy">
        <div class="pp-feature-kicker">Analisi competitor</div>
        <h2>Capisci se stai vendendo sotto mercato.</h2>
        <p>Confronta strutture simili, legge la media mercato e segnala quando il tuo prezzo rischia di lasciare revenue sul tavolo.</p>
        <div class="pp-feature-points">
          <span>Confronto prezzi su immobili comparabili</span>
          <span>Posizione sopra, sotto o in linea con il mercato</span>
          <span>Motivazioni leggibili per ogni raccomandazione</span>
        </div>
      </div>
      <div class="pp-mini-shot">
        <div class="pp-shot-label">Market position</div>
        <div class="pp-mini-shot-grid">
          <div class="pp-mini-card"><small>Tu</small><b>&euro;145</b></div>
          <div class="pp-mini-card"><small>Media mercato</small><b>&euro;158</b></div>
          <div class="pp-mini-card"><small>Gap revenue</small><b style="color:#B5523A">+9%</b></div>
          <div class="pp-mini-card"><small>Competitor letti</small><b>14</b></div>
        </div>
      </div>
    </section>
    <section class="pp-feature-row reverse">
      <div class="pp-feature-copy">
        <div class="pp-feature-kicker">Occupancy & stagionalita</div>
        <h2>Prezzi sensibili alla domanda reale.</h2>
        <p>Weekend, eventi, stagionalita e occupazione cambiano il valore di una notte. PricePilot li traduce in range prezzo controllati.</p>
        <div class="pp-feature-points">
          <span>Ricalcolo periodico ogni 6 ore</span>
          <span>Range minimo e massimo sempre rispettati</span>
          <span>Override manuali per date speciali</span>
        </div>
      </div>
      <div class="pp-mini-shot dark">
        <div class="pp-shot-label">Demand curve</div>
        <div class="pp-bars-clean">
          <i style="height:32%"></i><i style="height:46%"></i><i class="brick" style="height:70%"></i><i class="brick" style="height:88%"></i><i style="height:58%"></i>
        </div>
        <div class="pp-table-clean" style="margin-top:18px">
          <div class="pp-row-clean"><span>Weekend</span><b>Alta</b><span>+14%</span></div>
          <div class="pp-row-clean"><span>Evento</span><b>Forte</b><span>+22%</span></div>
          <div class="pp-row-clean"><span>Occupancy</span><b>81%</b><span>+8%</span></div>
        </div>
      </div>
    </section>
    <section class="pp-feature-row">
      <div class="pp-feature-copy">
        <div class="pp-feature-kicker">Telegram approval</div>
        <h2>Automazione con controllo umano.</h2>
        <p>Nel piano Plus ricevi una proposta prezzo motivata. Approvi o rifiuti da Telegram, poi PricePilot applica il cambio sulle OTA quando l'integrazione e collegata.</p>
        <div class="pp-feature-points">
          <span>Prezzo attuale, prezzo suggerito e motivo</span>
          <span>Approva o rifiuta con un click</span>
          <span>Storico decisioni sempre consultabile</span>
        </div>
      </div>
      <div class="pp-mini-shot dark">
        <div class="pp-telegram-clean">
          <h4>PricePilot · proposta prezzo</h4>
          <p>Villa Centro<br><strong>&euro;145 &rarr; &euro;172</strong><br>Motivo: competitor + evento + alta domanda.</p>
          <div class="pp-tg-actions"><span>Approva</span><span>Rifiuta</span></div>
        </div>
      </div>
    </section>
    <section class="pp-feature-row reverse">
      <div class="pp-feature-copy">
        <div class="pp-feature-kicker">Autopilot & analytics</div>
        <h2>Dal consiglio alla gestione automatica.</h2>
        <p>Il piano Pro applica i cambi prezzo rispettando guardrail e invia un riepilogo decisionale. Tu vedi cosa e successo, perche e con quale impatto stimato.</p>
        <div class="pp-feature-points">
          <span>Autopilot con limiti di sicurezza</span>
          <span>Revenue forecast e storico decisioni</span>
          <span>Gestione multi proprieta per portfolio piu grandi</span>
        </div>
      </div>
      <div class="pp-mini-shot">
        <div class="pp-shot-label">Portfolio overview</div>
        <div class="pp-mini-shot-grid">
          <div class="pp-mini-card"><small>Proprieta</small><b>8</b></div>
          <div class="pp-mini-card"><small>Decisioni oggi</small><b>23</b></div>
          <div class="pp-mini-card"><small>Applicate</small><b style="color:#B5523A">19</b></div>
          <div class="pp-mini-card"><small>In attesa</small><b>4</b></div>
        </div>
      </div>
    </section>
    """, unsafe_allow_html=True)
    return
    st.markdown('<span id="funzionalita"></span>', unsafe_allow_html=True)
    st.markdown(
        '<section class="pp-section"><h2>Funzionalita orientate al ricavo</h2>'
        '<p class="pp-section-lead">Non solo controlli tecnici: ogni funzione serve a decidere '
        'prima, vendere meglio e ridurre il lavoro manuale sulla gestione prezzi.</p></section>',
        unsafe_allow_html=True,
    )
    features = [
        ("📈", "Prezzi sempre aggiornati", "PricePilot ricalcola i prezzi in base a mercato, domanda e occupazione."),
        ("🏘️", "Analisi competitor", "Confronta strutture simili e capisce se sei sopra, sotto o in linea col mercato."),
        ("📲", "Telegram approval", "Nel piano Plus approvi ogni cambio prezzo da Telegram prima che venga applicato."),
        ("🤖", "Autopilot completo", "Nel piano Pro PricePilot aggiorna i prezzi automaticamente e ti invia il riepilogo."),
        ("📅", "Calendario smart", "Visualizzi prezzi consigliati, weekend, eventi, override e giorni bloccati."),
        ("🏢", "Multi proprieta", "Gestisci piu appartamenti con strategie e regole diverse."),
    ]
    for row in range(0, len(features), 3):
        cols = st.columns(3)
        for col, item in zip(cols, features[row:row + 3]):
            with col:
                _feature_card(*item)


def _feature_card(icon: str, title: str, desc: str):
    st.markdown(
        f'<div class="pp-card"><div class="pp-icon">{_html.escape(icon)}</div>'
        f'<h3>{_html.escape(title)}</h3><p>{_html.escape(desc)}</p></div>',
        unsafe_allow_html=True,
    )


def _render_how_it_works_section():
    st.markdown('<span id="come-funziona"></span>', unsafe_allow_html=True)
    st.markdown("""
    <section class="pp-section">
      <h2>Tre passaggi, nessun caos operativo.</h2>
      <p class="pp-section-lead">La piattaforma resta semplice per chi ha una proprieta e scalabile per chi gestisce un portfolio.</p>
      <div class="pp-timeline">
        <div class="pp-time-step">
          <strong>1</strong>
          <h3>Collega OTA</h3>
          <p>Inserisci proprieta, citta, range prezzo e canali. PricePilot conosce subito i limiti commerciali.</p>
        </div>
        <div class="pp-time-step">
          <strong>2</strong>
          <h3>Analizza il mercato</h3>
          <p>Competitor, occupancy, eventi e stagionalita vengono trasformati in una raccomandazione leggibile.</p>
        </div>
        <div class="pp-time-step">
          <strong>3</strong>
          <h3>Approvi o automatizzi</h3>
          <p>Free suggerisce. Plus chiede conferma da Telegram. Pro lavora in autopilot con report.</p>
        </div>
      </div>
    </section>
    """, unsafe_allow_html=True)
    return
    st.markdown('<span id="come-funziona"></span>', unsafe_allow_html=True)
    st.markdown(
        '<section class="pp-section"><h2>Come funziona</h2>'
        '<p class="pp-section-lead">Dal primo appartamento all autopilot: PricePilot mantiene '
        'il controllo chiaro e ti mostra sempre il motivo di ogni prezzo.</p></section>',
        unsafe_allow_html=True,
    )
    steps = [
        ("1", "Collega la proprieta", "Inserisci OTA, citta, prezzo minimo e massimo.",
         [("OTA", "Airbnb / Booking"), ("Zona", "Centro storico"), ("Range", "&euro;80 - &euro;220")]),
        ("2", "PricePilot analizza il mercato", "Competitor, occupancy, eventi, weekend e stagionalita.",
         [("Competitor", "14 simili"), ("Domanda", "Alta"), ("Evento", "Fiera weekend")]),
        ("3", "Approvi o automatizzi", "Free: cambi manualmente. Plus: approvi da Telegram. Pro: autopilot completo.",
         [("Free", "Suggerisce"), ("Plus", "Approvi"), ("Pro", "Applica")]),
    ]
    cols = st.columns(3)
    for col, (num, title, desc, preview) in zip(cols, steps):
        rows = "".join(
            f'<div class="pp-preview-row"><span>{label}</span><strong>{value}</strong></div>'
            for label, value in preview
        )
        with col:
            st.markdown(
                f'<div class="pp-step"><div class="pp-step-num">{num}</div>'
                f'<h3>{_html.escape(title)}</h3><p>{_html.escape(desc)}</p>'
                f'<div class="pp-step-preview">{rows}</div></div>',
                unsafe_allow_html=True,
            )


def _render_pricing_section():
    st.markdown('<span id="prezzi"></span>', unsafe_allow_html=True)
    st.markdown(
        '<section class="pp-section"><h2>Piani chiari per ogni livello di automazione.</h2>'
        '<p class="pp-section-lead">Free ti aiuta a decidere. Plus ti fa approvare da Telegram. '
        'Pro lascia lavorare PricePilot in autonomia con report e guardrail.</p></section>',
        unsafe_allow_html=True,
    )
    annual = st.toggle("Mostra prezzo annuale", value=False, key="public_pricing_annual")
    cols = st.columns(3)
    _pricing_card(
        cols[0],
        "free",
        "EUR 0",
        "Manual pricing assistant",
        "Suggerimenti prezzo e dashboard per aggiornare manualmente le OTA.",
        ["1 proprieta", "Analisi competitor", "Suggerimenti prezzo", "Calendario smart", "Aggiornamenti ogni 6h"],
        "Inizia Gratis",
        mode="Suggerisce",
        missing=["Telegram approval", "Aggiornamento automatico OTA"],
    )
    plus_price = "EUR 23/mese" if annual else "EUR 29/mese"
    _pricing_card(
        cols[1],
        "plus",
        plus_price,
        "Telegram approval",
        "Approvi il cambio da Telegram e PricePilot applica sulle OTA dopo conferma.",
        ["Fino a 5 proprieta", "Tutto del Free", "Approval Telegram", "Aggiornamento OTA dopo conferma", "Guardrail di sicurezza"],
        "Scegli Plus",
        recommended=True,
        mode="Approvi e applica",
    )
    pro_price = "EUR 63/mese" if annual else "EUR 79/mese"
    _pricing_card(
        cols[2],
        "pro",
        pro_price,
        "Full autopilot",
        "PricePilot aggiorna automaticamente i prezzi e invia report decisionali.",
        ["Fino a 25 proprieta", "Tutto del Plus", "Autopilot completo", "Report Telegram", "Supporto prioritario"],
        "Scegli Pro",
        mode="Fa tutto",
    )
    return
    st.markdown('<span id="prezzi"></span>', unsafe_allow_html=True)
    st.markdown(
        '<section class="pp-section"><h2>Scegli quanto vuoi automatizzare</h2>'
        '<p class="pp-section-lead"><strong>Free suggerisce.</strong> '
        '<strong>Plus ti chiede conferma e applica.</strong> '
        '<strong>Pro lavora in autonomia.</strong> Parti leggero e aumenta il livello '
        'di automazione quando sei pronto.</p></section>',
        unsafe_allow_html=True,
    )
    annual = st.toggle("Mostra prezzo annuale", value=False, key="public_pricing_annual")
    cols = st.columns(3)
    _pricing_card(cols[0], "free", "EUR 0", "Manual pricing assistant",
    "PricePilot analizza il mercato e ti suggerisce il prezzo. Tu lo aggiorni manualmente sulle OTA.", [
        "1 proprieta",
        "Analisi competitor",
        "Suggerimenti prezzo motivati",
        "Calendario smart",
        "Dashboard base",
        "Aggiornamenti ogni 6h",
    ], "Inizia Gratis", mode="Suggerisce", missing=["Telegram approval", "Aggiornamento automatico OTA"])
    plus_price = "EUR 23/mese" if annual else "EUR 29/mese"
    _pricing_card(cols[1], "plus", plus_price, "Telegram approval automation",
    "PricePilot genera il prezzo, ti chiede conferma su Telegram e dopo approvazione aggiorna automaticamente le OTA.", [
        "Fino a 5 proprieta",
        "Tutto del Free",
        "Approvazione Telegram",
        "Aggiornamento OTA dopo approvazione",
        "Analytics avanzate",
        "Guardrail di sicurezza",
    ], "Scegli Plus", recommended=True, mode="Approvi e applica")
    pro_price = "EUR 63/mese" if annual else "EUR 79/mese"
    _pricing_card(cols[2], "pro", pro_price, "Full autopilot revenue management",
    "PricePilot aggiorna automaticamente i prezzi sulle OTA e ti invia un report decisionale.", [
        "Fino a 25 proprieta",
        "Tutto del Plus",
        "Autopilot completo",
        "Report Telegram automatici",
        "Strategie avanzate",
        "Supporto prioritario",
    ], "Scegli Pro", mode="Fa tutto")


def _pricing_card(
    col,
    plan: str,
    price: str,
    subtitle: str,
    desc: str,
    features: list[str],
    cta: str,
    recommended: bool = False,
    mode: str = "",
    missing: list[str] | None = None,
):
    with col:
        badge = '<span class="pp-badge">Consigliato</span>' if recommended else ""
        feature_items = "".join(f"<li>{_html.escape(item)}</li>" for item in features)
        missing_items = "".join(f"<li>{_html.escape(item)}</li>" for item in (missing or []))
        missing_html = f'<ul class="pp-missing-list">{missing_items}</ul>' if missing_items else ""
        st.markdown(
            f'<div class="pp-price {"recommended" if recommended else ""}">{badge}'
            f'<div class="pp-price-name">{get_plan(plan)["label"]}</div>'
            f'<div class="pp-price-sub">{_html.escape(subtitle)}</div>'
            f'<div class="pp-price-value">{_html.escape(price)}</div>'
            f'<div class="pp-price-desc">{_html.escape(desc)}</div>'
            f'<div class="pp-plan-mode">{_html.escape(mode)}</div>'
            f'<ul class="pp-feature-list">{feature_items}</ul>{missing_html}</div>',
            unsafe_allow_html=True,
        )
        if st.button(cta, key=f"public_price_{plan}", use_container_width=True, type="primary" if recommended else "secondary"):
            _go_public("register", plan)


def _render_faq_section():
    st.markdown('<span id="faq"></span>', unsafe_allow_html=True)
    st.markdown(
        '<section class="pp-section" style="padding-bottom:28px !important"><h2>Domande frequenti.</h2>'
        '<p class="pp-section-lead">Risposte pratiche prima di collegare OTA, channel manager o automazioni reali.</p></section>'
        '<div class="pp-faq-wrap">',
        unsafe_allow_html=True,
    )
    with st.expander("PricePilot cambia gia i prezzi sulle OTA?"):
        st.write("Free no: ricevi suggerimenti e aggiorni manualmente. Plus applica dopo approvazione Telegram. Pro applica automaticamente quando le integrazioni OTA/channel manager sono collegate.")
    with st.expander("Come funziona Telegram approval?"):
        st.write("Ricevi prezzo attuale, prezzo suggerito e motivazione. Approvi o rifiuti con un click, senza aprire la dashboard.")
    with st.expander("Airbnb, Booking e channel manager sono supportati?"):
        st.write("PricePilot e progettato per lavorare con OTA e channel manager tramite API/PMS. La landing mostra il flusso previsto; le integrazioni reali si collegano tramite provider.")
    with st.expander("Posso usarlo con una sola proprieta?"):
        st.write("Si. Il piano Free e pensato per partire con una proprieta e capire subito come PricePilot ragiona sui prezzi.")
    with st.expander("Ogni quanto analizza il mercato?"):
        st.write("Il ciclo operativo puo analizzare mercato e proprieta ogni 6 ore, in base al piano e alle impostazioni.")
    st.markdown("</div>", unsafe_allow_html=True)
    return
    st.markdown('<span id="faq"></span>', unsafe_allow_html=True)
    st.markdown(
        '<section class="pp-section"><h2>Domande frequenti</h2>'
        '<p class="pp-section-lead">Le risposte che un host o property manager deve avere prima di automatizzare i prezzi.</p></section>',
        unsafe_allow_html=True,
    )
    with st.expander("PricePilot cambia gia i prezzi sulle OTA?"):
        st.write("Nel piano Free no: ricevi suggerimenti e aggiorni manualmente. Nel piano Plus PricePilot aggiorna dopo approvazione Telegram. Nel piano Pro aggiorna automaticamente.")
    with st.expander("Come funziona Telegram?"):
        st.write("Ricevi una proposta prezzo con motivazione. Puoi approvare o rifiutare con un click.")
    with st.expander("Airbnb e supportato?"):
        st.write("PricePilot e progettato per lavorare con OTA e channel manager. Le integrazioni reali possono essere collegate tramite API/PMS/channel manager.")
    with st.expander("Posso usarlo con una sola proprieta?"):
        st.write("Si, il piano Free e pensato proprio per iniziare con una proprieta.")
    with st.expander("Ogni quanto aggiorna i prezzi?"):
        st.write("Il sistema puo analizzare il mercato ogni 6 ore, in base al piano e alle impostazioni.")


def _render_final_cta_section():
    st.markdown(
        '<section class="pp-final-cta">'
        '<h2>Revenue management professionale, senza complessita.</h2>'
        '<p>Parti con i suggerimenti. Passa all approvazione Telegram o all autopilot quando vuoi automatizzare davvero.</p>'
        '</section>',
        unsafe_allow_html=True,
    )
    _, col, _ = st.columns([1.2, 1, 1.2])
    with col:
        if st.button("Inizia Gratis", key="final_start_free", use_container_width=True, type="primary"):
            _go_public("register", "free")
    return
    st.markdown(
        '<section class="pp-final-cta">'
        '<h2>Smetti di inseguire il mercato. Lascia che PricePilot lavori per te.</h2>'
        '<p>Parti con una proprieta. Passa a Plus o Pro quando vuoi automatizzare.</p>'
        '</section>',
        unsafe_allow_html=True,
    )
    _, col, _ = st.columns([1, 1, 1])
    with col:
        if st.button("Inizia Gratis", key="final_start_free", use_container_width=True, type="primary"):
            _go_public("register", "free")


def _reset_auth_scroll():
    components.html(
        """
        <script>
        (() => {
          try {
            const w = window.parent || window;
            const cleanUrl = w.location.pathname + w.location.search;
            if (w.location.hash) {
              w.history.replaceState(null, "", cleanUrl);
            }
            w.scrollTo({ top: 0, left: 0, behavior: "instant" });
            setTimeout(() => w.scrollTo(0, 0), 40);
            setTimeout(() => w.scrollTo(0, 0), 160);
          } catch (error) {}
        })();
        </script>
        """,
        height=0,
    )


def _render_auth_panel(client, view: str):
    _reset_auth_scroll()

    _, col, _ = st.columns([1, 1.25, 1])
    with col:
        plan = _selected_plan()
        with st.container(border=True, key=f"pp_auth_card_{view}"):
            st.markdown(
                f'<span class="pp-plan-pill">Piano scelto: {get_plan(plan)["label"]}</span>'
                f'<div class="pp-auth-title">{_auth_title(view)}</div>'
                f'<div class="pp-auth-copy">{_auth_copy(view)}</div>',
                unsafe_allow_html=True,
            )

            if view == "login":
                login_email = st.text_input("Email", key="auth_login_email", placeholder="mario@esempio.it")
                login_pw = st.text_input("Password", key="auth_login_pw", type="password", placeholder="Password")
                if st.button("Accedi", key="auth_login_btn", use_container_width=True, type="primary"):
                    _do_login(client, login_email, login_pw)
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Crea account", key="auth_to_register", use_container_width=True):
                        _go_public("register")
                with c2:
                    if st.button("Password dimenticata", key="auth_to_forgot", use_container_width=True):
                        _go_public("forgot")

            elif view == "forgot":
                reset_email = st.text_input("Email", key="auth_forgot_email", placeholder="mario@esempio.it")
                if st.button("Invia link di recupero", key="auth_forgot_btn", use_container_width=True, type="primary"):
                    _do_password_reset(client, reset_email)
                if st.button("Torna al login", key="forgot_to_login", use_container_width=True):
                    _go_public("login")

            else:
                signup_email = st.text_input("Email", key="auth_signup_email", placeholder="mario@esempio.it")
                signup_name = st.text_input("Nome attivita", key="auth_signup_account_name", placeholder="Es. Rossi Apartments")
                selected_plan = st.selectbox(
                    "Piano scelto",
                    list(PLAN_ORDER),
                    index=list(PLAN_ORDER).index(plan),
                    format_func=lambda p: get_plan(p)["label"],
                    key="auth_signup_plan",
                )
                st.session_state[_KEY_SELECTED_PLAN] = selected_plan
                signup_pw = st.text_input(
                    "Password",
                    key="auth_signup_pw",
                    type="password",
                    placeholder="Minimo 6 caratteri",
                )
                if st.button("Crea account", key="auth_signup_btn", use_container_width=True, type="primary"):
                    _do_signup(client, signup_email, signup_pw, signup_name, selected_plan)
                if st.button("Hai gia un account? Accedi", key="register_to_login", use_container_width=True):
                    _go_public("login")

            auth_label = "Supabase" if client else "locale"
            st.caption(f"Auth {auth_label}. Dopo la registrazione entrerai nell onboarding iniziale.")
            if st.button("Torna alla home", key="auth_back_home", use_container_width=True):
                _go_public("landing")


def _auth_title(view: str) -> str:
    return {
        "login": "Accedi alla dashboard",
        "forgot": "Recupera password",
        "register": "Crea il tuo account",
    }.get(view, "Crea il tuo account")


def _auth_copy(view: str) -> str:
    return {
        "login": "Bentornato. Entra nella dashboard per gestire proprieta, decisioni e prezzi.",
        "forgot": "Inserisci la tua email. Con Supabase collegato riceverai il link di recupero.",
        "register": "Scegli il piano, crea l account e completa il setup della prima proprieta.",
    }.get(view, "")


def _do_login(client, email: str, password: str):
    email = (email or "").strip().lower()
    password = (password or "").strip()
    if not email or not password:
        st.error("Inserisci email e password.")
        return
    if client is None:
        _do_local_login(email, password)
        return
    try:
        resp = client.auth.sign_in_with_password({"email": email, "password": password})
        _store_supabase_session(resp)
        st.success("Accesso effettuato.")
        st.rerun()
    except Exception as exc:
        _handle_auth_error(exc, context="login")


def _do_signup(client, email: str, password: str, account_name: str = "", plan: str = "free"):
    email = (email or "").strip().lower()
    password = (password or "").strip()
    if not email or not password:
        st.error("Inserisci email e password.")
        return
    if len(password) < 6:
        st.error("La password deve avere almeno 6 caratteri.")
        return
    if client is None:
        _do_local_signup(email, password, account_name, plan)
        return
    try:
        selected_plan = normalize_plan(plan)
        resp = client.auth.sign_up({
            "email": email,
            "password": password,
            "options": {
                "data": {
                    "account_name": account_name or "La mia attivita",
                    "plan": selected_plan,
                }
            },
        })
        user = getattr(resp, "user", None)
        session = getattr(resp, "session", None)
        if user and getattr(user, "id", None) and session:
            _store_supabase_session(resp, account_name=account_name, plan=selected_plan)
            st.success("Account creato.")
            st.rerun()
        elif user and getattr(user, "id", None):
            _ensure_external_user(
                email=getattr(user, "email", email),
                external_user_id=getattr(user, "id", ""),
                plan=selected_plan,
                account_name=account_name,
            )
            st.info("Registrazione completata. Controlla la email e poi accedi.")
        else:
            st.info("Registrazione completata. Controlla la email e poi accedi.")
    except Exception as exc:
        _handle_auth_error(exc, context="registrazione")


def _store_supabase_session(resp, account_name: str = "", plan: str | None = None):
    user = getattr(resp, "user", None)
    session = getattr(resp, "session", None)
    if user:
        metadata = getattr(user, "user_metadata", {}) or {}
        local_user = _ensure_external_user(
            email=getattr(user, "email", ""),
            external_user_id=getattr(user, "id", ""),
            plan=plan or metadata.get("plan") or _selected_plan(),
            account_name=account_name or metadata.get("account_name", ""),
        )
        _set_local_session(local_user)
    if session:
        st.session_state[_KEY_SESSION] = session


def _hash_password(password: str) -> str:
    iterations = 210_000
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, expected = (stored or "").split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations),
        ).hex()
        return secrets.compare_digest(digest, expected)
    except Exception:
        return False


def _set_local_session(user: dict | None):
    if not user:
        return
    st.session_state[_KEY_USER] = {
        "id": int(user["id"]),
        "email": user.get("email", ""),
        "account_id": int(user.get("account_id") or 1),
        "role": user.get("role", "owner"),
    }


def _do_local_login(email: str, password: str):
    user = get_user_by_email(email)
    if not user or not _verify_password(password, user.get("password_hash", "")):
        st.error("Email o password non corretti.")
        return
    update_user(int(user["id"]), {"last_login_at": datetime.utcnow().isoformat()})
    _set_local_session(user)
    st.success("Accesso effettuato.")
    st.rerun()


def _do_local_signup(email: str, password: str, account_name: str = "", plan: str = "free"):
    try:
        result = create_account_owner(
            email=email,
            password_hash=_hash_password(password),
            account_name=account_name or "La mia attivita",
            plan=plan,
        )
        _set_local_session(result["user"])
        st.success("Account creato. Benvenuto in PricePilot.")
        st.rerun()
    except ValueError as exc:
        st.error(str(exc))
    except Exception as exc:
        st.error(f"Errore durante la registrazione: {exc}")


def _ensure_external_user(
    email: str,
    external_user_id: str = "",
    plan: str = "free",
    account_name: str = "",
) -> dict | None:
    email = (email or "").strip().lower()
    if not email:
        return None
    existing = get_user_by_email(email)
    if existing:
        update_user(int(existing["id"]), {
            "auth_provider": "supabase",
            "external_user_id": external_user_id,
            "last_login_at": datetime.utcnow().isoformat(),
        })
        return get_user_by_email(email)

    account = create_account(account_name or "La mia attivita", plan=normalize_plan(plan), billing_status="dev")
    user = create_user(
        int(account["id"]),
        email=email,
        role="owner",
        full_name="",
    )
    update_user(int(user["id"]), {
        "auth_provider": "supabase",
        "external_user_id": external_user_id,
        "last_login_at": datetime.utcnow().isoformat(),
    })
    return get_user_by_email(email)


def _do_password_reset(client, email: str):
    email = (email or "").strip().lower()
    if not email:
        st.error("Inserisci la tua email.")
        return
    if client is None:
        st.info("Recupero password email disponibile quando collegheremo Supabase/SMTP. In locale puoi creare un nuovo account di test.")
        return
    try:
        client.auth.reset_password_email(email)
        st.success("Ti abbiamo inviato il link di recupero password.")
    except Exception as exc:
        _handle_auth_error(exc, context="recupero password")


def _handle_auth_error(exc: Exception, context: str = ""):
    msg = str(exc).lower()
    if "invalid login" in msg or "invalid credentials" in msg:
        st.error("Email o password non corretti.")
    elif "email not confirmed" in msg:
        st.warning("Conferma la tua email prima di accedere.")
    elif "already registered" in msg:
        st.error("Esiste gia un account con questa email. Usa Accedi.")
    elif "rate limit" in msg:
        st.error("Troppi tentativi. Riprova tra qualche minuto.")
    else:
        st.error(f"Errore durante la {context}: {exc}")
