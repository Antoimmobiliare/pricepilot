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
from datetime import datetime
from typing import Optional, Dict, Any

logger = logging.getLogger("pricepilot.telegram_bot")

WEBHOOK_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


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
) -> Dict:
    """
    Invia il messaggio di richiesta approvazione con i pulsanti inline
    ✅ Approva / ❌ Rifiuta.

    Returns il risultato dell'API (contiene 'result.message_id' se ok).
    """
    pct   = (new_price - old_price) / max(old_price, 1) * 100
    arrow = "🔼" if pct > 0 else ("🔽" if pct < 0 else "➡️")
    event_line  = f"\n🎉 *Evento:* `{event}`" if event and event not in ("none", "", "0") else ""
    reason_line = f"\n\n📋 *Motivo:*\n{reason}" if reason else ""

    text = (
        f"✈️ *PricePilot – Approvazione Richiesta*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏠 *Proprietà:* {prop_name}\n"
        f"📅 *Notte:* {target_date or 'vedi motivo'}\n"
        f"💰 *Prezzo attuale:* €{old_price:.2f}\n"
        f"🎯 *Prezzo consigliato:* €{new_price:.2f} {arrow} `{pct:+.1f}%`\n"
        + (f"📊 *Media mercato:* €{market_avg:.2f}\n" if market_avg is not None else "📋 Analisi calendario proprio; competitor da verificare manualmente.\n")
        +
        f"📈 *Occupancy:* {occupancy * 100:.0f}%"
        f"{event_line}"
        f"{reason_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Vuoi applicare il nuovo prezzo al listing?"
    )

    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Approva",  "callback_data": f"approve_{log_id}"},
            {"text": "❌ Rifiuta",  "callback_data": f"reject_{log_id}"},
        ]]
    }
    if next_log_id:
        keyboard['inline_keyboard'].append([{'text': 'Prossima data da valutare', 'callback_data': f'review_{next_log_id}'}])

    result = _api_call("sendMessage", {
        "chat_id":      chat_id,
        "text":         text,
        "parse_mode":   "Markdown",
        "reply_markup": keyboard,
    })

    # Salva il message_id nel decision_log per l'edit successivo
    if result.get("ok") and "result" in result:
        from pricepilot.core.database import update_decision_tg_message
        msg_id = result["result"].get("message_id")
        if msg_id:
            update_decision_tg_message(log_id, msg_id)

    return result


def send_cycle_digest(account_id: int, results: list) -> dict:
    """One plain-text overview per property; each nightly change is approved separately."""
    from pricepilot.core.database import (get_property, get_telegram_link_by_property,
                                          get_notification_preferences, record_notification_log)
    grouped = {}
    for row in results:
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
        ok = bool(response.get('ok'))
        sent += int(ok)
        failed += int(not ok)
        record_notification_log(event_type='pricing_cycle_digest', status='sent' if ok else 'failed',
            account_id=account_id, property_id=property_id, recipient=str(link['chat_id']),
            payload={'decision_ids': [r['log_id'] for r in rows]}, error='' if ok else 'Telegram delivery not confirmed')
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
    if not current or current.get('decision_log_id') != log_id:
        return False
    candidates = get_decision_log(limit=1000, property_id=context['property_id'], account_id=context['account_id'])
    remaining = []
    for candidate in candidates:
        state = candidate.get('decision') or ''
        if candidate['id'] == log_id or candidate['date'] <= row['date'] or not state.startswith('PENDING_APPROVAL') or '[' in state:
            continue
        pointer = get_calendar_price(context['property_id'], candidate['date'], context['account_id'])
        if pointer and pointer.get('decision_log_id') == candidate['id']:
            remaining.append(candidate)
    remaining.sort(key=lambda c: c['date'])
    from pricepilot.engine.decision_engine import _scoped_property
    prop = _scoped_property(context['property_id'], context['account_id']) or {}
    send_approval_request(log_id, prop.get('name', ''), row['old_price'], row['new_price'], row['occupancy'],
                          row.get('market_avg'), '', chat_id, row.get('notes', ''), row['date'],
                          remaining[0]['id'] if remaining else None)
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


def process_webhook(update: Dict) -> None:
    """
    Punto di ingresso per gli aggiornamenti Telegram (webhook o polling).
    Gestisce messaggi /start e callback_query dai pulsanti inline.
    """
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

            _handle_callback(cq_id, data, chat_id, message_id, orig_text)

    except Exception as exc:
        logger.error(f"Errore process_webhook: {exc}", exc_info=True)


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
