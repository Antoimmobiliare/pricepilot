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
    env = os.environ.get("PRICEPILOT_ENV", "").strip().lower()
    explicit = os.environ.get("PRICEPILOT_REQUIRE_TELEGRAM_WEBHOOK_SECRET", "").strip().lower()
    return (
        bool(get_webhook_secret())
        or env in {"prod", "production"}
        or explicit in {"1", "true", "yes", "on"}
    )


def verify_webhook_secret(header_value: str | None) -> bool:
    secret = get_webhook_secret()
    if not secret:
        return not webhook_secret_required()
    return secrets.compare_digest(header_value or "", secret)


# ─── Chiamate API Telegram (via urllib, zero dipendenze) ─────────────────────

def _api_call(method: str, payload: Dict[str, Any]) -> Dict:
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
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
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
        f"💰 *Prezzo attuale:* €{old_price:.2f}\n"
        f"🎯 *Prezzo consigliato:* €{new_price:.2f} {arrow} `{pct:+.1f}%`\n"
        f"📊 *Media mercato:* €{market_avg:.2f}\n"
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
    from pricepilot.core.database import get_conn

    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, account_id, property_id FROM decision_log WHERE id=?",
            (log_id,),
        ).fetchone()
        if not row:
            return None
        link = conn.execute("""
            SELECT id FROM telegram_links
            WHERE property_id=? AND chat_id=? AND active=1
            ORDER BY id DESC LIMIT 1
        """, (row["property_id"], chat_id)).fetchone()
        if not link:
            return None
    return {
        "id": int(row["id"]),
        "account_id": int(row["account_id"] or 1),
        "property_id": int(row["property_id"]),
    }


def _handle_callback(
    callback_query_id: str,
    data: str,
    chat_id: int,
    message_id: int,
    original_text: str,
) -> None:
    """Gestisce i pulsanti inline ✅ Approva / ❌ Rifiuta."""
    from pricepilot.core.database import get_conn, update_calendar_status_for_decision
    from pricepilot.engine.decision_engine import approve_decision

    if data.startswith("approve_"):
        try:
            log_id = int(data.split("_", 1)[1])
        except (ValueError, IndexError):
            answer_callback_query(callback_query_id, "❌ ID decisione non valido")
            return

        context = _decision_context_for_chat(log_id, chat_id)
        if not context:
            answer_callback_query(callback_query_id, "Decisione non disponibile per questa chat")
            return

        result = approve_decision(log_id, account_id=context["account_id"])
        answer_callback_query(callback_query_id, "Prezzo approvato. Sincronizzalo manualmente sul canale.")
        edit_message_text(
            chat_id, message_id,
            original_text + "\n\n*APPROVATO* - prezzo da aggiornare manualmente sul listing."
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

        context = _decision_context_for_chat(log_id, chat_id)
        if not context:
            answer_callback_query(callback_query_id, "Decisione non disponibile per questa chat")
            return

        with get_conn() as conn:
            conn.execute(
                "UPDATE decision_log SET applied=0, decision=decision||' [REJECTED]' "
                "WHERE id=? AND account_id=?",
                (log_id, context["account_id"]),
            )
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
            resp = _api_call("getUpdates", {
                "offset":          offset,
                "timeout":         timeout,
                "allowed_updates": ["message", "callback_query"],
            })
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
