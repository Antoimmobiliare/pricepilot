"""
PricePilot - Telegram Bot Service
Bot centralizzato per notifiche e approvazioni dinamiche del prezzo.

Flusso operativo:
  1. L'operatore genera un "link di collegamento" dalla dashboard
     → viene creato un token univoco nella tabella telegram_links
  2. L'operatore condivide il deep link (t.me/BotName?start=TOKEN) con sé stesso
     o con il gestore della proprietà
  3. L'utente apre Telegram, clicca il link → il bot riceve /start TOKEN
     → il bot salva il chat_id e conferma il collegamento
  4. Ogni volta che il Decision Engine genera una raccomandazione in modalità
     "approval", viene inviato un messaggio Telegram con pulsanti inline
     ✅ Approva / ❌ Rifiuta
  5. La risposta ai pulsanti aggiorna il decision_log e facoltativamente
     il listing via channel manager API

Avvio polling (sviluppo):
    python -m pricepilot.services.telegram_bot

Webhook (produzione):
    uvicorn pricepilot.api.server:app --reload --port 8000
    # Il webhook viene registrato automaticamente se APP_BASE_URL è impostato
"""
import os
import json
import time
import hashlib
import secrets
import logging
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Dict, Any

logger = logging.getLogger("pricepilot.telegram_bot")
# Bump this module marker when the Telegram delivery path changes so hosted
# Streamlit runtimes invalidate any previously loaded module copy.
TELEGRAM_DELIVERY_MODULE_VERSION = "2026-10-05-decision-ux"

WEBHOOK_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"

_SIGNAL_LABELS = {
    "occupancy_weak": "Occupancy debole",
    "occupancy_strong": "Occupancy forte",
    "pickup_weak": "Pickup debole",
    "pickup_strong": "Pickup forte",
    "confirmed_isolated_gap": "Gap isolato confermato",
}

_URGENCY_ACTION_LABELS = {
    "amplify_negative_signals": "Pressione alla vendita applicata entro i guardrail.",
    "hold_insufficient_negative_evidence": "Evidenza insufficiente: nessuna pressione last-minute aggiuntiva.",
    "hold_positive_signals": "Segnali positivi: nessuna pressione alla vendita aggiuntiva.",
    "hold_signal_conflict": "Segnali contrastanti: evitata ulteriore pressione last-minute.",
    "standard_rules_only": "Fascia standard: applicate soltanto le regole base.",
    "hold_no_negative_signals": "Nessun segnale negativo affidabile: prezzo protetto.",
    "disabled_legacy": "Applicate le regole base configurate.",
}

_ITALIAN_MONTHS = (
    "", "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
)


# ─── Helpers per le variabili d'ambiente ─────────────────────────────────────

def get_bot_token() -> str:
    """Legge il token del bot da variabile d'ambiente."""
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def get_bot_username() -> str:
    """Legge lo username del bot da variabile d'ambiente."""
    return os.environ.get("TELEGRAM_BOT_USERNAME", "pricepilot_bot").strip()


def is_configured() -> bool:
    """True se il bot token è configurato."""
    return bool(get_bot_token())


def get_webhook_secret() -> str:
    """Secret condiviso con Telegram per autenticare il webhook."""
    return (
        os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
        or os.environ.get("PRICEPILOT_TELEGRAM_WEBHOOK_SECRET", "").strip()
    )


def webhook_secret_required() -> bool:
    from pricepilot.core.data_quality import live_mode
    explicit = os.environ.get("PRICEPILOT_REQUIRE_TELEGRAM_WEBHOOK_SECRET", "").strip().lower()
    return (
        bool(get_webhook_secret())
        or live_mode()
        or explicit in {"1", "true", "yes", "on"}
    )


def verify_webhook_secret(header_value: str | None) -> bool:
    secret = get_webhook_secret()
    if not secret:
        return not webhook_secret_required()
    return secrets.compare_digest(header_value or "", secret)


# ─── Chiamate API Telegram (via urllib, zero dipendenze) ─────────────────────

def _api_call(
    method: str,
    payload: Dict[str, Any],
    *,
    request_timeout: int = 10,
) -> Dict:
    """Esegue una chiamata all'API Telegram Bot."""
    token = get_bot_token()
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN non configurato.")
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set"}

    url  = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=request_timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if (method == 'answerCallbackQuery' and e.code == 400
                and 'query is too old' in body.lower()):
            logger.info('Telegram ACK scaduto; esito economico indipendente, aggiornamento messaggio ancora consentito.')
            return {'ok': False, 'non_blocking': True, 'error_code': 'callback_ack_expired'}
        if e.code == 400 and "message is not modified" in body.lower():
            logger.info("Telegram edit ignorato: messaggio gia aggiornato.")
            return {"ok": True, "ignored": "message_not_modified", "description": body}
        logger.error(f"Telegram HTTP {e.code}: {body}")
        return {"ok": False, "error": body}
    except Exception as exc:
        logger.error(f"Telegram API error ({method}): {exc}")
        return {"ok": False, "error": str(exc)}


# ─── Link generation ─────────────────────────────────────────────────────────

def generate_link_token(property_id: int) -> str:
    """
    Genera un token nel formato: connect_<property_id>_<hex12>
    Il prefisso 'connect_<prop_id>' permette al bot di estrarre
    il property_id direttamente dal parametro /start senza lookup DB aggiuntivo.
    """
    rand = secrets.token_hex(8)
    return f"connect_{property_id}_{rand}"


def get_deep_link(token: str) -> str:
    """Ritorna il deep link Telegram per il token dato."""
    username = get_bot_username()
    return f"https://t.me/{username}?start={token}"


def _parse_start_token(text: str) -> str:
    """
    Estrae il token dal testo del comando /start.
    Gestisce sia il formato nuovo 'connect_<id>_<hex>' sia i token legacy.
    """
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def create_property_link(property_id: int) -> Dict:
    """
    Genera un nuovo token, lo salva nel DB e ritorna info sul link.

    Returns:
        {
          "link_id":   <id nel DB>,
          "token":     "abc123...",
          "deep_link": "https://t.me/BotName?start=abc123...",
          "property_id": <id>,
        }
    """
    from pricepilot.core.database import (
        save_telegram_link, revoke_telegram_link, get_property
    )

    # Disattiva eventuali link precedenti per questa proprietà
    revoke_telegram_link(property_id)

    token   = generate_link_token(property_id)
    link_id = save_telegram_link({
        "property_id": property_id,
        "token":       token,
        "active":      1,
    })

    return {
        "link_id":     link_id,
        "token":       token,
        "deep_link":   get_deep_link(token),
        "property_id": property_id,
    }


# ─── Invio messaggi ───────────────────────────────────────────────────────────

def send_message(chat_id: int, text: str, parse_mode: str = "Markdown") -> Dict:
    """Invia un messaggio Telegram semplice."""
    return _api_call("sendMessage", {
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": parse_mode,
    })


def _format_eur(value: float, *, signed: bool = False) -> str:
    amount = float(value)
    sign = "+" if signed and amount > 0 else ("-" if signed and amount < 0 else "")
    rendered = f"{abs(amount):.2f}".replace(".", ",")
    return f"{sign}€{rendered}"


def _format_target_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value or "Data non disponibile")
    return f"{parsed.day} {_ITALIAN_MONTHS[parsed.month]} {parsed.year}"


def _format_lead_time(hours: Any) -> Optional[str]:
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours < 0:
        return None
    total_minutes = int(round(float(hours) * 60))
    return f"{total_minutes // 60}h {total_minutes % 60:02d}m"


def _unsold_risk_label(value: Any) -> Optional[str]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    pressure = float(value)
    if pressure == 0:
        return "nessuno"
    if pressure <= .15:
        return "basso"
    if pressure <= .35:
        return "moderato"
    return "elevato"


def _signal_names(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    names = []
    for item in items:
        signal_id = item.get("signal") if isinstance(item, dict) else item
        if signal_id in _SIGNAL_LABELS and _SIGNAL_LABELS[signal_id] not in names:
            names.append(_SIGNAL_LABELS[signal_id])
    return names


def _decision_factors(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _display_reason(value: Any) -> str:
    """Remove storage metadata while preserving the engine's original reason."""
    notes = str(value or "").strip()
    if not notes.startswith("plan=") or " | guardrails=" not in notes:
        return notes
    guardrail_start = notes.find(" | guardrails=")
    reason_start = notes.find(" | ", guardrail_start + len(" | guardrails="))
    return notes[reason_start + 3:].strip() if reason_start >= 0 else notes


def _build_approval_payload(
    *, log_id: int, prop_name: str, old_price: float, new_price: float,
    occupancy: Optional[float], market_avg: Optional[float], event: str,
    reason: str, target_date: str, decision_factors: Optional[Dict[str, Any]] = None,
    next_log_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Render a decision already made by the engine; never derive a new price."""
    factors = _decision_factors(decision_factors)
    delta = float(new_price) - float(old_price)
    pct = delta / max(float(old_price), 1) * 100
    unchanged = abs(delta) < .005
    if unchanged:
        price_line = f"💶 *{_format_eur(old_price)} — MANTIENI*"
    else:
        price_line = f"💶 *{_format_eur(old_price)} → {_format_eur(new_price)}*"

    lines = [
        f"🏠 *{prop_name or 'Proprietà'}*",
        f"📅 {_format_target_date(target_date)}",
        "",
    ]
    human_time = _format_lead_time(factors.get("hours_until_checkin"))
    band = str(factors.get("lead_time_band") or factors.get("urgency_band") or "").strip()
    band_label = band.replace("_", " ")
    if human_time or band_label:
        lead_parts = ([f"Check-in tra {human_time}"] if human_time else []) + ([band_label] if band_label else [])
        lines.extend([f"⏱ {' · '.join(lead_parts)}", ""])
    pct_text = "0,0%" if unchanged else f"{pct:+.1f}%".replace(".", ",")
    lines.extend([
        price_line,
        f"Variazione: {_format_eur(delta, signed=True)} / {pct_text}",
    ])

    signal_lines = []
    if isinstance(occupancy, (int, float)) and not isinstance(occupancy, bool):
        signal_lines.append(f"Occupancy: {float(occupancy) * 100:.0f}%")
    pickup = factors.get("pickup_7d_nights")
    if isinstance(pickup, int) and not isinstance(pickup, bool):
        signal_lines.append(f"Pickup 7gg: {pickup} {'notte' if pickup == 1 else 'notti'}")
    negative = _signal_names(factors.get("negative_signals"))
    positive = _signal_names(factors.get("positive_signals"))
    if "Gap isolato confermato" in negative:
        gap_nights = factors.get("gap_nights")
        gap_suffix = f" ({gap_nights} {'notte' if gap_nights == 1 else 'notti'})" if isinstance(gap_nights, int) else ""
        signal_lines.append(f"Gap isolato: confermato{gap_suffix}")
    risk_label = _unsold_risk_label(factors.get("unsold_risk_pressure"))
    if risk_label:
        signal_lines.append(f"Rischio invenduto: {risk_label}")
    confidence = {"limited": "limitata", "supported": "supportata", "conflicted": "contrastante"}.get(
        str(factors.get("rules_confidence") or "")
    )
    if confidence:
        signal_lines.append(f"Evidenza: {confidence}")
    if negative:
        signal_lines.append(f"Segnali negativi: {', '.join(negative)}")
    if positive:
        signal_lines.append(f"Segnali positivi: {', '.join(positive)}")
    if signal_lines:
        lines.extend(["", "📊 *Segnali*", *signal_lines])

    if factors.get("signal_conflict") is True:
        lines.extend(["", "⚠️ *Segnali contrastanti*"])
        if positive:
            lines.append(f"Positivi: {', '.join(positive)}")
        if negative:
            lines.append(f"Negativi: {', '.join(negative)}")
        lines.append("PricePilot ha evitato ulteriore pressione last-minute.")

    action = _URGENCY_ACTION_LABELS.get(str(factors.get("urgency_action") or ""))
    explanation = str(reason or "").strip()
    if action or explanation:
        lines.extend(["", "💡 *PricePilot*"])
        if action:
            lines.append(action)
        if explanation:
            lines.append(explanation)
    elif unchanged:
        lines.extend(["", "💡 *PricePilot*", "I segnali disponibili non giustificano una variazione."])

    if isinstance(market_avg, (int, float)) and not isinstance(market_avg, bool) and market_avg > 0:
        lines.extend(["", f"Mercato osservato nella decisione: {_format_eur(market_avg)}"])
    if event and event not in ("none", "", "0"):
        lines.append(f"Evento registrato: {event}")

    keyboard = {"inline_keyboard": [[
        {"text": f"✅ Approva {_format_eur(new_price)}", "callback_data": f"approve_{log_id}"},
        {"text": "❌ Rifiuta", "callback_data": f"reject_{log_id}"},
    ]]}
    if next_log_id:
        keyboard["inline_keyboard"].append([
            {"text": "Prossima data da valutare", "callback_data": f"review_{next_log_id}"}
        ])
    return {"text": "\n".join(lines), "reply_markup": keyboard}


def send_approval_request(
    log_id:     int,
    prop_name:  str,
    old_price:  float,
    new_price:  float,
    occupancy:  float,
    market_avg: float,
    event:      str,
    chat_id:    int,
    reason:     str = "",
    target_date: str = "",
    next_log_id: Optional[int] = None,
    decision_factors: Optional[Dict[str, Any]] = None,
) -> Dict:
    """
    Invia il messaggio di richiesta approvazione con i pulsanti inline
    ✅ Approva / ❌ Rifiuta.

    Returns il risultato dell'API (contiene 'result.message_id' se ok).
    """
    rendered = _build_approval_payload(
        log_id=log_id, prop_name=prop_name, old_price=old_price, new_price=new_price,
        occupancy=occupancy, market_avg=market_avg, event=event, reason=reason,
        target_date=target_date, decision_factors=decision_factors,
        next_log_id=next_log_id,
    )

    result = _api_call("sendMessage", {
        "chat_id":      chat_id,
        "text":         rendered["text"],
        "parse_mode":   "Markdown",
        "reply_markup": rendered["reply_markup"],
    })

    # Salva il message_id nel decision_log per l'edit successivo
    if result.get("ok") and "result" in result:
        from pricepilot.core.database import update_decision_tg_message
        msg_id = result["result"].get("message_id")
        if msg_id:
            update_decision_tg_message(log_id, msg_id)

    return result


def send_existing_pending_approval(log_id: int, account_id: int) -> Dict:
    """Deliver one existing live pending decision, without creating a duplicate.

    This is intentionally limited to the notification leg.  It never reads or
    writes a channel manager and refuses sandbox, applied, rejected or already
    delivered decisions.
    """
    from pricepilot.core.database import (
        get_decision_log_entry, get_property, get_telegram_link_by_property,
        get_notification_preferences, get_calendar_price, record_notification_log,
    )

    row = get_decision_log_entry(int(log_id), account_id=int(account_id))
    if not row:
        return {"ok": False, "error": "decision_not_found"}
    decision = str(row.get("decision") or "")
    if row.get("data_source") == "test_sandbox":
        return {"ok": False, "error": "sandbox_decision_not_operational"}
    if row.get("applied") or not decision.startswith("PENDING_APPROVAL") or any(
        tag in decision for tag in ("[REJECTED]", "[APPROVED", "[APPLYING]")
    ):
        return {"ok": False, "error": "decision_not_pending"}
    if row.get("tg_message_id"):
        return {"ok": True, "already_sent": True,
                "message_id": str(row.get("tg_message_id"))}

    property_id = int(row.get("property_id") or 0)
    calendar = get_calendar_price(property_id, str(row.get("date") or ""), int(account_id))
    if (not calendar or calendar.get("decision_log_id") != int(log_id)
            or str(calendar.get("status") or "") != "pending_approval"):
        return {"ok": False, "error": "decision_not_pending"}
    prop = get_property(property_id, account_id=int(account_id))
    link = get_telegram_link_by_property(property_id)
    prefs = get_notification_preferences(int(account_id), property_id)
    if not prop or not link or not link.get("chat_id"):
        return {"ok": False, "error": "telegram_chat_not_connected"}
    if not int(prefs.get("telegram_enabled", 1)) or not int(prefs.get("approval_alerts", 1)):
        return {"ok": False, "error": "telegram_notifications_disabled"}

    result = send_approval_request(
        log_id=int(log_id), prop_name=prop.get("name", "Proprieta"),
        old_price=float(row.get("old_price") or 0), new_price=float(row.get("new_price") or 0),
        occupancy=(float(row["occupancy"]) if row.get("occupancy") is not None else None),
        market_avg=row.get("market_avg"), event="", chat_id=link["chat_id"],
        reason=_display_reason(row.get("notes")),
        target_date=str(row.get("date") or ""),
        decision_factors=_decision_factors(row.get("factors")),
    )
    message_id = (result.get("result") or {}).get("message_id") if result.get("ok") else None
    if not result.get("ok") or not message_id:
        safe_error = str(result.get("error") or "telegram_message_id_missing")
        record_notification_log(
            event_type="approval_request", status="failed", account_id=int(account_id),
            property_id=property_id, recipient=str(link["chat_id"]), error=safe_error,
            payload={"log_id": int(log_id), "new_price": row.get("new_price")},
        )
        return {"ok": False, "error": safe_error}
    record_notification_log(
        event_type="approval_request", status="sent", account_id=int(account_id),
        property_id=property_id, recipient=str(link["chat_id"]),
        message_id=str(message_id), payload={"log_id": int(log_id), "new_price": row.get("new_price")},
    )
    return {"ok": True, "message_id": str(message_id), "log_id": int(log_id)}


def create_test_approval(property_id: int, account_id: int) -> Dict:
    """Create and deliver one explicitly sandboxed Telegram approval.

    This path is deliberately separate from the pricing engine: it can only
    select a fresh, open Beds24 snapshot row at the reference price (EUR 89),
    and its callbacks never call the channel-manager writer.
    """
    from pricepilot.core.database import save_decision_log
    from pricepilot.services.operational_store import get_snapshot
    from pricepilot.core.database import get_property

    prop = get_property(int(property_id), account_id=int(account_id))
    link = _decision_link_for_property(int(property_id))
    if not prop or not link or not link.get("chat_id"):
        raise ValueError("Telegram non collegato alla proprietà richiesta.")
    snapshot = get_snapshot(int(account_id), int(property_id))
    if not snapshot or not snapshot.get("valid"):
        raise ValueError("Snapshot Beds24 assente o non valido.")
    try:
        observed = datetime.fromisoformat(str(snapshot["observed_at"]).replace("Z", "+00:00"))
        if observed.tzinfo is None or not 0 <= (datetime.now(timezone.utc) - observed).total_seconds() <= 6 * 3600:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ValueError("Snapshot Beds24 assente, scaduto o non verificabile.") from None
    today = date.today().isoformat()
    reservations = snapshot.get("reservations") or []
    from pricepilot.providers.registry import get_event_provider
    event_provider = get_event_provider()
    candidates = []
    for row in snapshot.get("inventory") or []:
        day = str(row.get("date") or "")
        if day < today or row.get("state") != "open" or row.get("current_price") is None:
            continue
        if abs(float(row.get("current_price")) - 89.0) > 0.005:
            continue
        if (row.get("arrival_restriction", "none") != "none" or row.get("booking_id")
                or type(row.get("min_stay")) is not int or row.get("min_stay") < 1):
            continue
        try:
            target = date.fromisoformat(day)
        except ValueError:
            continue
        if event_provider.event_for_property(prop=prop, target_date=target, account_id=int(account_id)):
            continue
        if any(b.get("status") in {"confirmed", "new", "request", "black"}
               and str(b.get("arrival", "")) <= day < str(b.get("departure", "")) for b in reservations):
            continue
        candidates.append((target, row))
    if not candidates:
        raise ValueError("Nessuna notte futura libera a EUR 89 verificata nello snapshot.")
    target, row = sorted(candidates, key=lambda item: item[0])[0]
    entry = {
        "account_id": int(account_id), "property_id": int(property_id),
        "old_price": 89.0, "new_price": 91.0, "market_avg": None,
        "occupancy": 0.0, "decision": "TEST_PENDING_APPROVAL", "mode": "approval",
        "applied": 0, "notes": "TEST TECNICO SANDBOX: nessuna raccomandazione reale; nessuna scrittura Beds24.",
        "date": target.isoformat(), "strategy": "test_sandbox", "factors": json.dumps({
            "test_only": True, "availability": "open", "numAvail": 1,
            "current_price": 89.0, "test_price": 91.0,
            "observed_at": snapshot["observed_at"],
        }), "current_price_source": "beds24_calendar", "data_source": "test_sandbox",
    }
    log_id = save_decision_log(entry)
    response = send_test_approval_request(log_id, prop.get("name", "Proprietà"), target.isoformat(), 89.0, 91.0,
                                          int(link["chat_id"]))
    if not response.get("ok"):
        raise ValueError("Telegram non ha confermato la consegna del test.")
    return {"log_id": log_id, "property_id": int(property_id), "date": target.isoformat(),
            "old_price": 89.0, "new_price": 91.0, "chat_id": int(link["chat_id"]), "telegram": response}


def _decision_link_for_property(property_id: int) -> Optional[Dict]:
    from pricepilot.core.database import get_telegram_link_by_property
    return get_telegram_link_by_property(int(property_id))


def send_test_approval_request(log_id: int, prop_name: str, target_date: str,
                               old_price: float, new_price: float, chat_id: int) -> Dict:
    """Send a clearly labelled sandbox approval; callbacks cannot write OTA."""
    pct = (new_price - old_price) / max(old_price, 1) * 100
    text = ("🧪 *PricePilot — TEST TECNICO SANDBOX*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🏠 *Proprietà:* {prop_name}\n📅 *Notte:* {target_date}\n"
            f"💰 *Prezzo corrente:* €{old_price:.2f}\n🎯 *Prezzo di test:* €{new_price:.2f} (`{pct:+.1f}%`)\n"
            "✅ Snapshot Beds24: open, numAvail=1, nessuna prenotazione/override\n"
            "⚠️ Questo è un test tecnico: non è una raccomandazione reale e non modifica Beds24.\n"
            "━━━━━━━━━━━━━━━━━━━━\nConfermi il test del flusso approval?")
    return _api_call("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "Markdown",
        "reply_markup": {"inline_keyboard": [[
            {"text": "✅ Approva TEST", "callback_data": f"test_approve_{log_id}"},
            {"text": "❌ Rifiuta TEST", "callback_data": f"test_reject_{log_id}"},
        ]]}})


def send_cycle_digest(account_id: int, results: list) -> dict:
    """One plain-text overview per property; each nightly change is approved separately."""
    from pricepilot.core.database import (get_property, get_telegram_link_by_property,
                                          get_notification_preferences, record_notification_log)
    grouped = {}
    for row in results:
        if row.get('data_source') == 'test_sandbox':
            continue
        if row.get('deduplicated') or row.get('calendar_status') in {'unchanged', 'locked'}:
            continue
        if row.get('mode') not in {'approval', 'advisory'}:
            continue
        property_id = row.get('property_id')
        if not property_id or not get_property(property_id, account_id=account_id):
            continue
        grouped.setdefault(property_id, []).append(row)
    sent, failed = 0, 0
    if not is_configured():
        return {'sent': 0, 'failed': 0, 'status': 'not_configured'}
    for property_id, rows in grouped.items():
        prefs = get_notification_preferences(account_id, property_id)
        if not int(prefs.get('telegram_enabled', 1)) or not int(prefs.get('approval_alerts', 1)):
            continue
        link = get_telegram_link_by_property(property_id)
        if not link or not link.get('chat_id'):
            continue
        rows.sort(key=lambda r: r['date'])
        text = f"PricePilot — {rows[0].get('property_name', '')}\n{len(rows)} proposte sul tuo calendario. Nessun prezzo modificato.\n\n"
        for row in rows[:12]:
            actions = (row.get('breakdown') or {}).get('manual_actions') or []
            if actions and row.get('calendar_status') == 'manual_review':
                action = actions[0]
                if (action.get('type') == 'minimum_stay_review'
                        or {'current_minimum_stay', 'suggested_minimum_stay'} <= set(action)):
                    text += (f"{row['date']}: prezzo invariato; valuta soggiorno minimo "
                             f"{action['current_minimum_stay']} → {action['suggested_minimum_stay']} notti\n")
                else:
                    text += (f"{row['date']}: prezzo invariato; occupazione e pickup "
                             "danno segnali contrari, verifica manualmente\n")
            else:
                text += f"{row['date']}: EUR {row['old_price']:.2f} → {row['recommended_price']:.2f}\n"
        if len(rows) > 12:
            text += f"Altre {len(rows)-12} date disponibili nella dashboard.\n"
        text += '\nApri i dettagli per valutare e approvare una notte alla volta. Le proposte scadono dopo 6 ore; disponibilità e regole vengono ricontrollate.'
        pending = [r for r in rows if r.get('mode') == 'approval'
                   and r.get('calendar_status') == 'pending_approval']
        payload = {'chat_id': link['chat_id'], 'text': text}
        if pending:
            payload['reply_markup'] = {'inline_keyboard': [[{'text': 'Esamina le proposte', 'callback_data': f"review_{pending[0]['log_id']}"}]]}
        response = _api_call('sendMessage', payload)
        message_id = (response.get('result') or {}).get('message_id')
        ok = bool(response.get('ok') and message_id)
        sent += int(ok)
        failed += int(not ok)
        record_notification_log(event_type='pricing_cycle_digest', status='sent' if ok else 'failed',
            account_id=account_id, property_id=property_id, recipient=str(link['chat_id']),
            message_id=str(message_id or ''),
            payload={'decision_ids': [r['log_id'] for r in rows]}, error='' if ok else 'Telegram delivery not confirmed')
        # Deliver the first actionable night through the existing approval sender.
        # The overview is not itself an approval request.
        if ok and pending:
            delivery = send_existing_pending_approval(pending[0]['log_id'], account_id)
            if not delivery.get('ok'):
                failed += 1
    return {'sent': sent, 'failed': failed}


def _review_pending(log_id, chat_id):
    from pricepilot.core.database import get_decision_log_entry, get_decision_log, get_calendar_price
    context = _decision_context_for_chat(log_id, chat_id)
    if not context:
        return False
    row = get_decision_log_entry(log_id, context['account_id'])
    if not row or not str(row.get('decision', '')).startswith('PENDING_APPROVAL') or any(t in row['decision'] for t in ('[REJECTED]', '[APPROVED_', '[APPLYING]')):
        return False
    from datetime import datetime, timezone
    stamp = datetime.fromisoformat(str(row['timestamp']).replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    if not 0 <= (datetime.now(timezone.utc)-stamp).total_seconds() <= 6*3600:
        return False
    current = get_calendar_price(context['property_id'], row['date'], context['account_id'])
    if (not current or current.get('decision_log_id') != log_id
            or current.get('status') != 'pending_approval'):
        return False
    if row.get('tg_message_id'):
        return True
    candidates = get_decision_log(limit=1000, property_id=context['property_id'], account_id=context['account_id'])
    remaining = []
    for candidate in candidates:
        state = candidate.get('decision') or ''
        if candidate['id'] == log_id or candidate['date'] <= row['date'] or not state.startswith('PENDING_APPROVAL') or '[' in state:
            continue
        pointer = get_calendar_price(context['property_id'], candidate['date'], context['account_id'])
        if (pointer and pointer.get('decision_log_id') == candidate['id']
                and pointer.get('status') == 'pending_approval'):
            remaining.append(candidate)
    remaining.sort(key=lambda c: c['date'])
    from pricepilot.engine.decision_engine import _scoped_property
    prop = _scoped_property(context['property_id'], context['account_id']) or {}
    result = send_approval_request(log_id, prop.get('name', ''), row['old_price'], row['new_price'], row.get('occupancy'),
                          row.get('market_avg'), '', chat_id, _display_reason(row.get('notes')), row['date'],
                          remaining[0]['id'] if remaining else None,
                          _decision_factors(row.get('factors')))
    if not result.get('ok') or not (result.get('result') or {}).get('message_id'):
        raise RuntimeError('Telegram non ha confermato la consegna della proposta individuale.')
    return True


def answer_callback_query(callback_query_id: str, text: str = "") -> Dict:
    """Risponde alla callback query (necessario entro 30s)."""
    return _api_call("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text":              text,
        "show_alert":        False,
    })


def edit_message_text(chat_id: int, message_id: int, text: str) -> Dict:
    """Modifica un messaggio già inviato (per aggiornare i pulsanti dopo la risposta)."""
    return _api_call("editMessageText", {
        "chat_id":    chat_id,
        "message_id": message_id,
        "text":       text,
        "parse_mode": "Markdown",
    })


def notify_auto_applied(
    chat_id:   int,
    prop_name: str,
    old_price: float,
    new_price: float,
    event:     str = "",
    reason:    str = "",
) -> Dict:
    """Notifica (senza pulsanti) per i prezzi applicati automaticamente."""
    pct   = (new_price - old_price) / max(old_price, 1) * 100
    arrow = "🔼" if pct > 0 else ("🔽" if pct < 0 else "➡️")
    event_line  = f"\n🎉 *Evento:* `{event}`" if event and event not in ("none", "", "0") else ""
    reason_line = f"\n📋 {reason}" if reason else ""

    text = (
        f"✈️ *PricePilot – Prezzo Aggiornato*\n"
        f"🏠 *{prop_name}*\n"
        f"💰 {old_price:.2f}€ → *{new_price:.2f}€* {arrow} `{pct:+.1f}%`"
        f"{event_line}"
        f"{reason_line}"
    )
    return send_message(chat_id, text)


# ─── Gestione aggiornamenti webhook / polling ─────────────────────────────────

def _handle_start(chat_id: int, username: str, token: str) -> None:
    """Gestisce il comando /start <token>: collega il chat_id alla proprietà."""
    from pricepilot.core.database import (
        get_telegram_link_by_token, save_telegram_link, get_property,
    )

    link = get_telegram_link_by_token(token)

    if not link:
        send_message(chat_id, "❌ *Link non valido o scaduto.*\nRigenera un nuovo link dalla dashboard.")
        return

    if not link.get("active"):
        send_message(chat_id, "⚠️ *Link revocato.* Genera un nuovo link dalla dashboard.")
        return

    if link.get("chat_id"):
        # Già collegato
        prop = get_property(link["property_id"]) or {}
        send_message(
            chat_id,
            f"ℹ️ Questo link è già associato alla proprietà *{prop.get('name', '')}*.\n"
            f"Se hai problemi, rigenera il link dalla dashboard."
        )
        return

    # Primo collegamento: salva il chat_id
    save_telegram_link({
        **link,
        "chat_id":           chat_id,
        "telegram_username": username,
    })

    prop = get_property(link["property_id"]) or {}
    prop_name = prop.get("name", "la tua proprietà")

    send_message(
        chat_id,
        f"✅ *Account collegato con successo!*\n\n"
        f"🏠 Proprietà: *{prop_name}*\n\n"
        f"D'ora in avanti riceverai qui le notifiche di pricing.\n"
        f"Per le decisioni in modalità *approval*, ti verrà chiesto\n"
        f"di approvare o rifiutare il nuovo prezzo con i pulsanti inline."
    )
    logger.info(f"Telegram collegato: property_id={link['property_id']} chat_id={chat_id}")


def _decision_context_for_chat(log_id: int, chat_id: int) -> Optional[Dict]:
    """Ritorna la decisione solo se la chat e collegata alla stessa proprieta."""
    from pricepilot.core.database import get_telegram_decision_context
    return get_telegram_decision_context(log_id, chat_id)


def _callback_decision_id(data: str) -> Optional[int]:
    """Estrae un id decisione senza mai autorizzare il callback.

    Questa funzione serve soltanto per collegare il callback alla traccia di
    audit. L'autorizzazione resta vincolata a ``_decision_context_for_chat``
    nel normale handler operativo.
    """
    if not isinstance(data, str):
        return None
    for prefix in ("approve_", "reject_", "test_approve_", "test_reject_", "review_"):
        if data.startswith(prefix):
            raw = data[len(prefix):]
            if prefix.startswith("test_"):
                raw = raw.split("_", 1)[-1]
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
    return None


def _callback_scope_for_audit(log_id: Optional[int], chat_id: Optional[int]) -> Dict[str, Any]:
    """Ritorna account/property per audit senza concedere accesso operativo."""
    if log_id is None:
        return {}
    try:
        if chat_id is not None:
            context = _decision_context_for_chat(log_id, chat_id)
            if context:
                return context
        from pricepilot.core.database import get_decision_log_entry
        row = get_decision_log_entry(log_id)
        if row:
            return {
                "account_id": row.get("account_id"),
                "property_id": row.get("property_id"),
            }
    except Exception as exc:
        logger.warning("Scope callback non disponibile per audit: %s", type(exc).__name__)
    return {}


def _sanitize_callback_error(exc: Exception) -> str:
    """Riduce gli errori persistiti evitando token e payload sensibili."""
    message = str(exc or "")
    token = get_bot_token()
    if token:
        message = message.replace(token, "[REDACTED]")
    return f"{type(exc).__name__}: {message[:500]}"


def _record_callback_audit(
    *,
    callback_query_id: str,
    message_id: Optional[int],
    callback_data: str,
    decision_log_id: Optional[int],
    chat_id: Optional[int],
    status: str,
    error: str = "",
) -> None:
    """Persist callback receipt/failure before operational processing.

    Audit failures are logged but never converted into a successful callback:
    the caller still re-raises processing failures and the endpoint returns a
    non-2xx response, allowing Telegram/monitoring to observe the failure.
    """
    scope = _callback_scope_for_audit(decision_log_id, chat_id)
    details = {
        "callback_query_id": str(callback_query_id or ""),
        "message_id": str(message_id or ""),
        "callback_data": str(callback_data or "")[:200],
        "decision_log_id": decision_log_id,
        "processing_state": status,
    }
    if error:
        details["error"] = error
    try:
        from pricepilot.core.database import record_audit_event
        record_audit_event(
            action="telegram_callback_received" if status == "received" else "telegram_callback_error",
            entity_type="telegram_callback",
            entity_id=str(callback_query_id or ""),
            account_id=int(scope.get("account_id") or 1),
            property_id=scope.get("property_id"),
            source="telegram",
            status=status,
            details=details,
        )
    except Exception as audit_exc:
        logger.error(
            "Audit callback Telegram non salvato (%s): %s",
            status, type(audit_exc).__name__, exc_info=True,
        )


def _record_approval_event(
    *,
    context: Dict,
    action: str,
    status: str,
    chat_id: int,
    message_id: int,
    callback_query_id: str,
    payload: Optional[Dict] = None,
    error: str = "",
) -> None:
    """Best-effort audit trail for Telegram approval/reject clicks."""
    try:
        from pricepilot.core.database import record_telegram_approval

        record_telegram_approval({
            "account_id": context["account_id"],
            "property_id": context["property_id"],
            "decision_log_id": context["id"],
            "telegram_link_id": context.get("telegram_link_id"),
            "chat_id": chat_id,
            "telegram_username": context.get("telegram_username", ""),
            "action": action,
            "status": status,
            "source": "telegram",
            "message_id": message_id,
            "callback_query_id": callback_query_id,
            "error": error,
            "payload": payload or {},
        })
    except Exception as exc:
        logger.warning("Storico approvazione Telegram non salvato: %s", exc)


def _handle_test_callback(callback_query_id: str, data: str, chat_id: int,
                          message_id: int, original_text: str) -> None:
    """Handle sandbox callbacks without invoking any operational writer."""
    from pricepilot.core.database import get_decision_log_entry, update_decision_state
    try:
        action, raw_id = data.split("_", 2)[1:]
        log_id = int(raw_id)
    except (ValueError, IndexError):
        answer_callback_query(callback_query_id, "Test non valido.")
        return
    context = _decision_context_for_chat(log_id, chat_id)
    row = get_decision_log_entry(log_id, context["account_id"] if context else None)
    if not context or not row or row.get("data_source") != "test_sandbox":
        answer_callback_query(callback_query_id, "Test non disponibile per questa chat.")
        return
    if any(marker in original_text for marker in ("TEST APPROVATO", "TEST RIFIUTATO", "TEST BLOCCATO")):
        answer_callback_query(callback_query_id, "Test gia gestito.")
        return
    try:
        stamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
        fresh = 0 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= 30 * 60
    except (KeyError, TypeError, ValueError):
        fresh = False
    valid_snapshot = False
    if fresh:
        try:
            from pricepilot.services.operational_store import get_snapshot
            snapshot = get_snapshot(int(context["account_id"]), int(context["property_id"]))
            for item in (snapshot or {}).get("inventory") or []:
                if (item.get("date") == row.get("date") and item.get("state") == "open"
                        and item.get("booking_id") in (None, "")
                        and item.get("arrival_restriction", "none") == "none"
                        and abs(float(item.get("current_price")) - float(row.get("old_price"))) <= .005):
                    valid_snapshot = True
                    break
        except (TypeError, ValueError, KeyError):
            valid_snapshot = False
    if not fresh or not valid_snapshot:
        status = "TEST_BLOCKED_STALE_OR_CHANGED"
        update_decision_state(log_id, account_id=context["account_id"], applied=False, decision=status)
        answer_callback_query(callback_query_id, "Test bloccato: snapshot scaduto o cambiato.")
        edit_message_text(chat_id, message_id, original_text + "\n\n*TEST BLOCCATO* — dati Beds24 cambiati o proposta scaduta.")
        _record_approval_event(context=context, action=action, status=status, chat_id=chat_id,
                               message_id=message_id, callback_query_id=callback_query_id)
        return
    status = "TEST_APPROVED_WRITE_GATE_BLOCKED" if action == "approve" else "TEST_REJECTED"
    update_decision_state(log_id, account_id=context["account_id"], applied=False, decision=status)
    answer_callback_query(callback_query_id, "Test approvato: write gate ancora disabilitato." if action == "approve" else "Test rifiutato.")
    final = ("*TEST APPROVATO* — nessuna scrittura eseguita (`PRICEPILOT_ALLOW_CHANNEL_WRITES=0`)."
             if action == "approve" else "*TEST RIFIUTATO* — nessuna scrittura eseguita.")
    edit_message_text(chat_id, message_id, original_text + "\n\n" + final)
    _record_approval_event(context=context, action=action, status=status, chat_id=chat_id,
                           message_id=message_id, callback_query_id=callback_query_id)


def _handle_callback(
    callback_query_id: str,
    data: str,
    chat_id: int,
    message_id: int,
    original_text: str,
) -> None:
    """Gestisce i pulsanti inline ✅ Approva / ❌ Rifiuta."""
    from pricepilot.core.database import mark_decision_rejected, update_calendar_status_for_decision
    from pricepilot.engine.decision_engine import approve_decision

    if data.startswith("test_approve_") or data.startswith("test_reject_"):
        _handle_test_callback(callback_query_id, data, chat_id, message_id, original_text)
        return

    if data.startswith('review_'):
        try:
            available = _review_pending(int(data.split('_', 1)[1]), chat_id)
        except (ValueError, KeyError, TypeError):
            available = False
        answer_callback_query(callback_query_id, 'Dettagli inviati.' if available else 'Proposta scaduta o non disponibile: ricalcolare.')
        return

    if data.startswith("approve_"):
        try:
            log_id = int(data.split("_", 1)[1])
        except (ValueError, IndexError):
            answer_callback_query(callback_query_id, "❌ ID decisione non valido")
            return

        if any(marker in original_text for marker in ("*APPROVATO*", "*RIFIUTATO*", "*NON APPLICATO*")):
            answer_callback_query(callback_query_id, "Decisione gia gestita.")
            return

        context = _decision_context_for_chat(log_id, chat_id)
        if not context:
            answer_callback_query(callback_query_id, "Decisione non disponibile per questa chat")
            return

        result = approve_decision(log_id, account_id=context["account_id"])
        _record_approval_event(
            context=context,
            action="approve",
            status=result.get("status", "approved"),
            chat_id=chat_id,
            message_id=message_id,
            callback_query_id=callback_query_id,
            payload=result,
            error="" if result.get("approved") else result.get("message", ""),
        )
        if not result.get('approved'):
            answer_callback_query(callback_query_id, 'Proposta non applicata: dati da aggiornare.')
            detail = result.get('message') or 'Proposta non piu approvabile. Ricalcola dalla dashboard.'
            edit_message_text(chat_id, message_id, original_text + f"\n\n*NON APPLICATO* - {detail}")
            return
        if result.get("applied"):
            callback_text = "Prezzo approvato e sincronizzato."
            final_line = "*APPROVATO* - prezzo sincronizzato sul channel manager."
        elif result.get("status") == "approved_sync_failed":
            callback_text = "Prezzo approvato, ma sync OTA fallita."
            final_line = "*APPROVATO* - sync OTA fallita: controlla integrazione e log."
        else:
            callback_text = "Prezzo approvato. Aggiorna manualmente il canale."
            final_line = "*APPROVATO* - sync OTA non ancora collegato: aggiorna manualmente il prezzo sul canale."

        answer_callback_query(callback_query_id, callback_text)
        edit_message_text(
            chat_id, message_id,
            original_text + f"\n\n{final_line}"
        )
        logger.info(
            f"Decisione {log_id} approvata via Telegram (chat_id={chat_id}, "
            f"status={result.get('status')})"
        )

    elif data.startswith("reject_"):
        try:
            log_id = int(data.split("_", 1)[1])
        except (ValueError, IndexError):
            answer_callback_query(callback_query_id, "❌ ID decisione non valido")
            return

        if any(marker in original_text for marker in ("*APPROVATO*", "*RIFIUTATO*", "*NON APPLICATO*")):
            answer_callback_query(callback_query_id, "Decisione gia gestita.")
            return

        context = _decision_context_for_chat(log_id, chat_id)
        if not context:
            answer_callback_query(callback_query_id, "Decisione non disponibile per questa chat")
            return

        rejected = mark_decision_rejected(log_id, context["account_id"])
        if not rejected:
            answer_callback_query(callback_query_id, 'Decisione gia gestita o invio in corso.')
            return
        update_calendar_status_for_decision(
            decision_log_id=log_id,
            status="rejected",
            applied_price=None,
            notes="Rifiutato da Telegram.",
        )
        answer_callback_query(callback_query_id, "❌ Prezzo rifiutato.")
        edit_message_text(
            chat_id, message_id,
            original_text + "\n\n❌ *RIFIUTATO* – il prezzo rimane invariato."
        )
        _record_approval_event(
            context=context,
            action="reject",
            status="rejected",
            chat_id=chat_id,
            message_id=message_id,
            callback_query_id=callback_query_id,
            payload={"decision_log_id": log_id, "reason": "telegram_reject"},
        )
        logger.info(f"Decisione {log_id} rifiutata via Telegram (chat_id={chat_id})")

    else:
        answer_callback_query(callback_query_id, "Azione non riconosciuta.")


def process_telegram_update(update: Dict) -> None:
    """
    Punto di ingresso per gli aggiornamenti Telegram (webhook o polling).
    Gestisce messaggi /start e callback_query dai pulsanti inline.
    """
    callback_meta: Optional[Dict[str, Any]] = None
    try:
        # ── Messaggi di testo ─────────────────────────────────────────────────
        if "message" in update:
            msg      = update["message"]
            text     = msg.get("text", "").strip()
            chat_id  = msg["chat"]["id"]
            username = msg.get("from", {}).get("username", "")

            if text.startswith("/start "):
                token = _parse_start_token(text)
                _handle_start(chat_id, username, token)
            elif text == "/start":
                send_message(
                    chat_id,
                    "👋 *Benvenuto su PricePilot!*\n\n"
                    "Per collegare una proprietà usa il link generato dalla dashboard.\n"
                    "Esempio: `https://t.me/BotName?start=TOKEN`"
                )
            elif text == "/status":
                send_message(chat_id, "✈️ *PricePilot Bot* è attivo e funzionante.")

        # ── Callback da pulsanti inline ───────────────────────────────────────
        elif "callback_query" in update:
            cq         = update["callback_query"]
            data       = cq.get("data", "")
            chat_id    = cq["message"]["chat"]["id"]
            message_id = cq["message"]["message_id"]
            cq_id      = cq["id"]
            orig_text  = cq["message"].get("text", "")

            callback_meta = {
                "callback_query_id": str(cq_id),
                "message_id": message_id,
                "callback_data": data,
                "decision_log_id": _callback_decision_id(data),
                "chat_id": chat_id,
            }
            # Persist the receipt before any approval/rejection operation.  A
            # later exception is recorded separately and is re-raised below.
            _record_callback_audit(status="received", **callback_meta)

            _handle_callback(cq_id, data, chat_id, message_id, orig_text)

    except Exception as exc:
        if callback_meta:
            safe_error = _sanitize_callback_error(exc)
            _record_callback_audit(status="error", error=safe_error, **callback_meta)
            try:
                # Telegram buttons otherwise spin indefinitely while the
                # callback is retried.  This acknowledgement does not mutate
                # PricePilot/Beds24 and is best-effort only.
                answer_callback_query(
                    callback_meta["callback_query_id"],
                    "PricePilot: errore interno, nessuna modifica applicata.",
                )
            except Exception as answer_exc:
                logger.error(
                    "Risposta errore callback Telegram non inviata: %s",
                    type(answer_exc).__name__, exc_info=True,
                )
        logger.error("Errore process_webhook: %s", _sanitize_callback_error(exc), exc_info=True)
        # Do not turn an operational failure into HTTP 200.  The API endpoint
        # maps this exception to HTTP 500 and monitoring/Telegram can observe
        # the failed delivery; idempotency remains enforced by the handler.
        raise


def process_webhook(update: Dict) -> None:
    """Backward-compatible HTTP entry point using the shared update handler."""
    process_telegram_update(update)


# ─── Polling (sviluppo locale) ────────────────────────────────────────────────

def poll_forever(timeout: int = 30) -> None:
    """
    Long-polling per ricevere aggiornamenti. Usato in sviluppo locale
    quando non è possibile configurare un webhook pubblico.

    Avvio: python -m pricepilot.services.telegram_bot
    """
    if not is_configured():
        logger.error("TELEGRAM_BOT_TOKEN non impostato. Imposta la variabile in .env")
        return

    logger.info(f"Telegram polling avviato (timeout={timeout}s) ...")
    offset = 0

    while True:
        try:
            resp = _api_call(
                "getUpdates",
                {
                    "offset":          offset,
                    "timeout":         timeout,
                    "allowed_updates": ["message", "callback_query"],
                },
                request_timeout=timeout + 10,
            )
            if resp.get("ok"):
                for upd in resp.get("result", []):
                    offset = upd["update_id"] + 1
                    try:
                        process_webhook(upd)
                    except Exception as exc:
                        logger.error(f"Errore su update {upd.get('update_id')}: {exc}")
            else:
                logger.warning(f"getUpdates non ok: {resp.get('description', resp)}")
                time.sleep(5)
        except KeyboardInterrupt:
            logger.info("Polling interrotto.")
            break
        except Exception as exc:
            logger.error(f"Polling error: {exc}")
            time.sleep(5)


# ─── Registrazione webhook (produzione) ───────────────────────────────────────

def set_webhook(base_url: str) -> Dict:
    """
    Registra il webhook su Telegram.
    Chiamare dopo il deploy con APP_BASE_URL impostato.
    """
    webhook_url = base_url.rstrip("/") + "/telegram/webhook"
    payload = {
        "url":              webhook_url,
        "allowed_updates":  ["message", "callback_query"],
        "drop_pending_updates": True,
    }
    secret = get_webhook_secret()
    if secret:
        payload["secret_token"] = secret
    result = _api_call("setWebhook", payload)
    logger.info(f"setWebhook → {result}")
    return result


def delete_webhook() -> Dict:
    return _api_call("deleteWebhook", {"drop_pending_updates": False})


def get_webhook_info() -> Dict:
    return _api_call("getWebhookInfo", {})


def get_bot_info() -> Dict:
    return _api_call("getMe", {})


# ─── Entry point per il polling standalone ───────────────────────────────────

if __name__ == "__main__":
    import sys

    os.environ.setdefault("PRICEPILOT_RUNTIME", "worker")

    # Assicura che il root del progetto sia nel path
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[3]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    # Carica .env
    from pricepilot.core.config import _load_dotenv  # type: ignore
    _load_dotenv()

    # Init DB
    from pricepilot.core.database import init_db
    init_db()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not is_configured():
        print("❌  TELEGRAM_BOT_TOKEN non impostato nel file .env")
        print("    Modifica .env e aggiungi: TELEGRAM_BOT_TOKEN=<il tuo token>")
        sys.exit(1)

    info = get_bot_info()
    if info.get("ok"):
        bot = info["result"]
        print(f"✅  Bot: @{bot['username']} ({bot['first_name']})")
        print(f"   ID: {bot['id']}")
    else:
        print(f"❌  Errore connessione bot: {info.get('error')}")
        sys.exit(1)

    print()
    print("🤖  PricePilot Telegram Bot – Polling avviato")
    print("    Premi Ctrl+C per fermare")
    print()
    poll_forever()
