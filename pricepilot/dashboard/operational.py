"""Operational views built only from persisted, account-scoped facts."""
from datetime import date, timedelta
from time import monotonic

import pandas as pd
import streamlit as st

from pricepilot.core.database import get_decision_log, get_operation_runs, get_price_calendar
from pricepilot.providers.registry import get_occupancy_provider
from pricepilot.services.operational_store import get_reservation_metrics
from pricepilot.services.readiness import property_readiness

UNKNOWN = "Non disponibile"
_READ_CACHE_TTL_SECONDS = 12


def _session_cached(name, key, loader):
    """Cache breve confinata alla sessione browser e quindi al suo account."""
    cache_key = f"_pp_operational_cache_{name}_{key!r}"
    cached = st.session_state.get(cache_key)
    now = monotonic()
    if cached and now - cached[0] < _READ_CACHE_TTL_SECONDS:
        return cached[1]
    value = loader()
    st.session_state[cache_key] = (now, value)
    return value


def _money(value):
    return UNKNOWN if value is None else f"€ {float(value):,.2f}"


def _percent(value):
    return UNKNOWN if value is None else f"{float(value):.1%}"


def _metrics(account_id, property_id, start, end):
    if not property_id:
        return None
    try:
        return _session_cached(
            "metrics", (account_id, property_id, start, end),
            lambda: get_reservation_metrics(account_id, property_id, start, end),
        )
    except (RuntimeError, ValueError):
        return None


def _render_metrics(metrics):
    if not metrics or not metrics.get("complete"):
        st.info("KPI non disponibili: serve uno snapshot completo di calendario e prenotazioni per il periodo.")
        return False
    cols = st.columns(5)
    cols[0].metric("Occupazione", _percent(metrics.get("occupancy")))
    cols[1].metric("Notti prenotate", str(metrics.get("booked_nights", 0)))
    cols[2].metric("ADR", _money(metrics.get("adr")))
    cols[3].metric("RevPAR", _money(metrics.get("revpar")))
    pickup = metrics.get("pickup_7d_nights")
    cols[4].metric("Nuove notti · 7 giorni", UNKNOWN if pickup is None else str(pickup))
    if metrics.get("adr") is None:
        st.caption(
            "ADR, RevPAR e ricavi restano non disponibili finché gli importi Beds24 non sono "
            "classificati e coperti per tutte le notti prenotate del periodo."
        )
    st.caption(
        f"Periodo: {metrics.get('booked_nights', 0)} notti prenotate su "
        f"{metrics.get('available_nights', 0)} vendibili. "
        "I blocchi proprietario, manutenzione e indisponibilità non entrano nel denominatore."
    )
    return True


def _readiness(account_id, property_id):
    return _session_cached(
        "readiness", (account_id, property_id),
        lambda: property_readiness(int(account_id), int(property_id)),
    )


def _calendar(account_id, property_id, start, end):
    return _session_cached(
        "calendar", (account_id, property_id, start, end),
        lambda: get_price_calendar(
            account_id=int(account_id), property_id=int(property_id),
            date_from=str(start), date_to=str(end), limit=1000,
        ),
    )


def _runs(account_id):
    return _session_cached(
        "runs", account_id,
        lambda: get_operation_runs(account_id=int(account_id), limit=20),
    )


def _decisions(account_id, property_id):
    return _session_cached(
        "decisions", (account_id, property_id),
        lambda: get_decision_log(account_id=int(account_id), property_id=int(property_id), limit=100),
    )


def _render_readiness(account_id, property_id):
    if not property_id:
        return
    try:
        readiness = _readiness(account_id, property_id)
    except (RuntimeError, ValueError):
        st.error("Impossibile verificare lo stato operativo dell’appartamento.")
        return
    labels = [
        ("Configurazione", readiness["configured"]),
        ("Analisi calendario", readiness["analysis_ready"]),
        ("Proposte Telegram", readiness["approval_ready"]),
        ("Invio prezzi", readiness["write_ready"]),
    ]
    cols = st.columns(len(labels))
    for column, (label, ok) in zip(cols, labels):
        column.metric(label, "Pronto" if ok else "Da completare")
    pending = [c for c in readiness["checks"] if not c["ok"]]
    if pending:
        with st.expander("Passaggi mancanti", expanded=not readiness["analysis_ready"]):
            for check in pending:
                st.write(f"• **{check['label']}** — {check['detail']}")


def _calendar_table(rows):
    fields = {
        "date": "Data", "current_price": "Prezzo letto",
        "recommended_price": "Prezzo proposto", "applied_price": "Prezzo inviato",
        "status": "Stato", "current_price_source": "Fonte prezzo",
        "updated_at": "Ultimo aggiornamento",
    }
    frame = pd.DataFrame(rows)
    columns = [key for key in fields if key in frame.columns]
    return frame[columns].rename(columns=fields)


def _open_properties_section():
    st.session_state["pp_main_section_label"] = "🏡 Proprietà"


def render(account_id, section, property_id=None):
    titles = {"home": "Stato operativo", "calendar": "Calendario e tariffe",
              "analytics": "Risultati dell’alloggio", "pricing": "Proposte di prezzo"}
    st.subheader(titles[section])
    if not property_id:
        st.info("Aggiungi o seleziona un appartamento per visualizzare i dati operativi.")
        st.button(
            "Aggiungi il primo appartamento",
            type="primary",
            width="stretch",
            key=f"operational_add_first_property_{section}",
            on_click=_open_properties_section,
        )
        return
    occupancy = get_occupancy_provider()
    source_ready = "unconfigured" not in occupancy.name
    if not source_ready:
        st.warning("Calendario reale non collegato: PricePilot non genera proposte.")
    st.caption(
        f"Fonte calendario: {occupancy.name}. Le proposte usano disponibilità, prenotazioni e regole "
        "dell’alloggio; la decisione finale resta tua."
    )
    today = date.today()
    period_end = today + timedelta(days=30)
    if section == "home":
        _render_readiness(account_id, property_id)
        st.markdown("### Prossimi 30 giorni")
        _render_metrics(_metrics(account_id, property_id, today, period_end))
    if section == "analytics":
        left, right = st.columns(2)
        start = left.date_input("Dal", today, key=f"analytics_start_{property_id}")
        end_inclusive = right.date_input("Al", today + timedelta(days=29), key=f"analytics_end_{property_id}")
        if end_inclusive < start:
            st.error("La data finale precede quella iniziale.")
            return
        _render_metrics(_metrics(account_id, property_id, start, end_inclusive + timedelta(days=1)))
    if section == "calendar":
        start = st.date_input("Dal", today, key=f"calendar_start_{property_id}")
        end = st.date_input("Al", today + timedelta(days=89), key=f"calendar_end_{property_id}")
        if end < start:
            st.error("La data finale precede quella iniziale.")
            return
        rows = _calendar(account_id, property_id, start.isoformat(), end.isoformat())
        if rows:
            st.dataframe(_calendar_table(rows), hide_index=True, width="stretch")
            st.caption("Prezzo letto, prezzo proposto e prezzo inviato sono valori distinti. L’invio confermato da Beds24 non certifica ancora la propagazione sulle OTA.")
        else:
            st.info("Nessuna tariffa reale registrata nel periodo.")
        return
    if section == "pricing":
        st.info("Il ciclo controlla le date future e crea solo proposte supportate da un calendario completo e aggiornato. Nessun prezzo viene inviato senza approvazione.")
        try:
            can_analyze = _readiness(account_id, property_id)["analysis_ready"]
        except (RuntimeError, ValueError):
            can_analyze = False
        if st.button("Avvia analisi", disabled=not can_analyze):
            from pricepilot.core.scheduler import run_pricing_cycle
            try:
                with st.spinner("Analisi in corso"):
                    result = run_pricing_cycle(account_id=account_id, source="dashboard_manual")
                st.write({"proposte": len(result.get("results", [])), "date non analizzate": len(result.get("errors", []))})
                if result.get("errors"):
                    st.warning("Analisi incompleta: alcune date non avevano dati o condizioni sufficienti.")
            except Exception:
                st.error("Analisi non completata. Controlla stato del calendario, regole e servizi.")
    if section in {"home", "pricing"}:
        runs = _runs(account_id)
        if runs:
            st.write("Ultimi cicli")
            st.dataframe(runs, hide_index=True, width="stretch")
    if section in {"pricing", "analytics"}:
        decisions = _decisions(account_id, property_id)
        if decisions:
            st.write("Ultime decisioni")
            st.dataframe(decisions, hide_index=True, width="stretch")
        else:
            st.info("Nessuna decisione registrata.")
    if section in {"home", "analytics"}:
        st.caption("I valori economici derivano esclusivamente dagli importi prenotazione classificati. PricePilot non stima ricavi moltiplicando una tariffa per le notti del mese.")
