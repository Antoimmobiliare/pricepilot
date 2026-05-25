"""
PricePilot - Notification System
Notifiche console e Telegram per aggiornamenti di prezzo.
"""
import logging
import sys
import urllib.request
import urllib.parse
import json
from typing import Optional

from pricepilot.core.config import CONFIG

logger = logging.getLogger("pricepilot.notifier")


def notify_console(message: str) -> None:
    """Stampa messaggio a console e nel log."""
    try:
        print(f"[PRICEPILOT] {message}")
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        safe = message.encode(enc, errors="replace").decode(enc)
        print(f"[PRICEPILOT] {safe}")
    logger.info(message)


def notify_telegram(message: str, token: str = None, chat_id: str = None) -> bool:
    """
    Invia messaggio via Telegram Bot API.
    Ritorna True se successo, False altrimenti.
    """
    tok  = token   or CONFIG.get("telegram_token", "")
    chat = chat_id or CONFIG.get("telegram_chat_id", "")

    if not tok or not chat:
        logger.debug(f"[TELEGRAM-DISABLED] {message}")
        return False

    url  = f"https://api.telegram.org/bot{tok}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id":    chat,
        "text":       message,
        "parse_mode": "Markdown",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                logger.info(f"[TELEGRAM OK] {message[:60]}")
                return True
    except Exception as e:
        logger.warning(f"[TELEGRAM ERROR] {e}")
    return False


def format_price_alert(date_str: str, old: float, new: float, event: str = "") -> str:
    pct  = round((new - old) / max(old, 1) * 100, 1)
    sign = "+" if pct >= 0 else ""
    evt_note = f" 🎯 Evento: {event}" if event and event not in ("none", "0", "") else ""
    return (
        f"🏠 *PricePilot Alert*\n"
        f"📅 Data: `{date_str}`\n"
        f"💰 Prezzo: `€{old:.2f}` → `€{new:.2f}` ({sign}{pct}%){evt_note}"
    )


def notify_price_change(date_str: str, old: float, new: float, event: str = "") -> None:
    msg = format_price_alert(date_str, old, new, event)
    notify_console(msg.replace("*", "").replace("`", ""))
    notify_telegram(msg)
