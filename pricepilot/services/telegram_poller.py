"""One-shot Telegram update poller for stateless GitHub Actions runners.

The poller owns transport only. Every valid Telegram update is passed to the
same ``process_telegram_update`` function used by the HTTP webhook; approval,
revalidation, atomic claim, channel write and read-back remain in the existing
PricePilot workflow.
"""
from __future__ import annotations

import argparse
import logging
import os
from typing import Any, Callable, Dict, Optional

from pricepilot.core.database import (
    advance_telegram_update_cursor,
    get_telegram_update_cursor,
    mark_telegram_update_failure,
)
from pricepilot.services.telegram_bot import (
    _api_call,
    _sanitize_callback_error,
    delete_webhook,
    get_webhook_info,
    process_telegram_update,
)

logger = logging.getLogger("pricepilot.telegram_poller")

DEFAULT_CONSUMER_KEY = "github-actions-telegram-approvals-v1"
DEFAULT_BATCH_LIMIT = 50


class TelegramPollingError(RuntimeError):
    """Fail-closed transport/storage failure."""


class WebhookPollingConflict(TelegramPollingError):
    """Raised when getUpdates would conflict with an active webhook."""


def _safe_error(exc: Exception) -> str:
    return _sanitize_callback_error(exc)


def _call_result_error(result: Dict[str, Any], operation: str) -> TelegramPollingError:
    description = str(result.get("description") or result.get("error") or "unknown error")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        description = description.replace(token, "[REDACTED]")
    return TelegramPollingError(f"Telegram {operation} fallita: {description[:500]}")


def verify_polling_mode(
    *,
    telegram_call: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Require an empty webhook URL before calling getUpdates."""
    if telegram_call is None:
        info = get_webhook_info()
    else:
        info = telegram_call("getWebhookInfo", {})
    if not info.get("ok"):
        raise _call_result_error(info, "getWebhookInfo")
    current = info.get("result") or {}
    url = str(current.get("url") or "").strip()
    if url:
        raise WebhookPollingConflict(
            "Webhook Telegram ancora attivo: disabilitarlo esplicitamente prima del polling."
        )
    return {
        "webhook_url": "",
        "pending_update_count": int(current.get("pending_update_count") or 0),
    }


def migrate_webhook_to_polling() -> Dict[str, Any]:
    """Explicitly delete the webhook without discarding pending updates."""
    before = get_webhook_info()
    if not before.get("ok"):
        raise _call_result_error(before, "getWebhookInfo")
    if str((before.get("result") or {}).get("url") or "").strip():
        deleted = delete_webhook()
        if not deleted.get("ok"):
            raise _call_result_error(deleted, "deleteWebhook")
    after = verify_polling_mode()
    return {"ok": True, **after}


def _classify_update(update: Dict[str, Any]) -> tuple[str, str]:
    """Classify structural poison updates without interpreting authorization."""
    if "callback_query" in update:
        callback = update.get("callback_query")
        if not isinstance(callback, dict):
            return "ignored_malformed", "callback_query non e un oggetto"
        message = callback.get("message")
        chat = message.get("chat") if isinstance(message, dict) else None
        if (
            not str(callback.get("id") or "").strip()
            or not isinstance(callback.get("data"), str)
            or not isinstance(message, dict)
            or not isinstance(chat, dict)
            or chat.get("id") is None
            or message.get("message_id") is None
        ):
            return "ignored_malformed", "callback_query incompleta"
        return "process", ""
    if "message" in update:
        message = update.get("message")
        chat = message.get("chat") if isinstance(message, dict) else None
        if not isinstance(message, dict) or not isinstance(chat, dict) or chat.get("id") is None:
            return "ignored_malformed", "message incompleto"
        return "process", ""
    return "ignored_unsupported", "tipo update non supportato"


def run_poll_once(
    *,
    consumer_key: str = DEFAULT_CONSUMER_KEY,
    batch_limit: int = DEFAULT_BATCH_LIMIT,
    telegram_call: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
    update_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
    before_cursor_commit: Optional[Callable[[Dict[str, Any]], None]] = None,
    require_no_webhook: bool = True,
) -> Dict[str, Any]:
    """Process one bounded getUpdates batch and then exit.

    The cursor advances only after the shared handler returns or after a
    structurally invalid update is explicitly classified as terminal poison.
    Operational exceptions leave the cursor unchanged so the update is retried.
    """
    if not 1 <= int(batch_limit) <= 100:
        raise ValueError("batch_limit deve essere tra 1 e 100")
    call = telegram_call or (lambda method, payload: _api_call(method, payload))
    handler = update_handler or process_telegram_update

    if require_no_webhook:
        verify_polling_mode(telegram_call=call)

    cursor = get_telegram_update_cursor(consumer_key)
    next_update_id = int(cursor.get("next_update_id") or 0)
    response = call(
        "getUpdates",
        {
            "offset": next_update_id,
            "limit": int(batch_limit),
            "timeout": 0,
            "allowed_updates": ["message", "callback_query"],
        },
    )
    if not response.get("ok"):
        raise _call_result_error(response, "getUpdates")
    updates = response.get("result") or []
    if not isinstance(updates, list):
        raise TelegramPollingError("Telegram getUpdates ha restituito un batch non valido.")

    processed = ignored = duplicates = 0
    for update in sorted(updates, key=lambda item: int(item.get("update_id", -1))):
        if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
            raise TelegramPollingError("Update Telegram senza update_id valido.")
        update_id = int(update["update_id"])
        if update_id < next_update_id:
            duplicates += 1
            continue

        classification, reason = _classify_update(update)
        try:
            if classification == "process":
                handler(update)
                status = "processed"
                processed += 1
            else:
                status = classification
                ignored += 1
                logger.warning("Update Telegram %s ignorato: %s", update_id, reason)

            if before_cursor_commit is not None:
                before_cursor_commit(update)
            advanced = advance_telegram_update_cursor(
                consumer_key,
                expected_next_update_id=next_update_id,
                next_update_id=update_id + 1,
                last_update_id=update_id,
                status=status,
                error=reason,
            )
            if not advanced:
                raise TelegramPollingError(
                    "Cursor Telegram modificato da un altro worker; batch interrotto."
                )
            next_update_id = update_id + 1
        except Exception as exc:
            safe_error = _safe_error(exc)
            try:
                mark_telegram_update_failure(
                    consumer_key,
                    expected_next_update_id=next_update_id,
                    update_id=update_id,
                    error=safe_error,
                )
            except Exception as cursor_exc:
                logger.error(
                    "Errore poller e mancata persistenza diagnostica: %s",
                    type(cursor_exc).__name__,
                )
            raise

    return {
        "ok": True,
        "consumer_key": consumer_key,
        "received": len(updates),
        "processed": processed,
        "ignored": ignored,
        "duplicates": duplicates,
        "next_update_id": next_update_id,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="PricePilot Telegram one-shot poller")
    parser.add_argument("--batch-limit", type=int, default=DEFAULT_BATCH_LIMIT)
    parser.add_argument("--consumer-key", default=DEFAULT_CONSUMER_KEY)
    parser.add_argument("--migrate-from-webhook", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)

    os.environ.setdefault("PRICEPILOT_RUNTIME", "worker")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        if args.migrate_from_webhook:
            result = migrate_webhook_to_polling()
            logger.info(
                "Webhook Telegram disabilitato; polling pronto (pending=%s).",
                result["pending_update_count"],
            )
        else:
            result = verify_polling_mode()
            logger.info("Polling Telegram verificato (pending=%s).", result["pending_update_count"])
        if args.verify_only:
            return 0
        result = run_poll_once(
            consumer_key=args.consumer_key,
            batch_limit=args.batch_limit,
            require_no_webhook=False,
        )
        logger.info(
            "Batch Telegram concluso: received=%s processed=%s ignored=%s duplicates=%s.",
            result["received"], result["processed"], result["ignored"], result["duplicates"],
        )
        return 0
    except Exception as exc:
        logger.error("Telegram poller fallito: %s", _safe_error(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
