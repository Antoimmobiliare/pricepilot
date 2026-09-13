"""Audit read-only dei servizi necessari prima del collegamento di Luma."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Il modulo di configurazione usa il loader .env interno, senza dipendenze.
from pricepilot.core import config as _config  # noqa: E402,F401


def _http_json(url: str, *, timeout: int = 20) -> tuple[int | None, dict | None]:
    try:
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "PricePilot-audit/1"})
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(256_000).decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
            return int(response.status), payload
    except HTTPError as exc:
        return int(exc.code), None
    except (URLError, TimeoutError, OSError):
        return None, None


def audit_supabase() -> dict:
    from pricepilot.core.supabase_client import get_supabase_admin_client

    client = get_supabase_admin_client()
    result = {"configured": client is not None, "tables": {}}
    if client is None:
        return result
    for table in ("accounts", "properties", "operational_documents", "pricing_date_locks"):
        try:
            client.table(table).select("*", count="exact").limit(1).execute()
            result["tables"][table] = True
        except Exception:
            result["tables"][table] = False
    result["ready"] = all(result["tables"].values())
    return result


def audit_telegram() -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return {"configured": False, "bot_reachable": False}
    status, payload = _http_json(f"https://api.telegram.org/bot{token}/getMe")
    return {
        "configured": True,
        "bot_reachable": status == 200 and bool((payload or {}).get("ok")),
        "http_status": status,
        "webhook_secret_configured": bool(os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()),
    }


def audit_api(base_url: str) -> dict:
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        return {"configured": False, "healthy": False}
    status, payload = _http_json(f"{base_url}/health", timeout=60)
    return {
        "configured": True,
        "healthy": status == 200 and isinstance(payload, dict),
        "http_status": status,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default=os.environ.get("PRICEPILOT_API_BASE_URL", ""))
    args = parser.parse_args()
    report = {
        "mode": "read_only",
        "supabase": audit_supabase(),
        "telegram": audit_telegram(),
        "api": audit_api(args.api_url),
        "scheduler_secret_configured": bool(os.environ.get("PRICEPILOT_SCHEDULER_KEY", "").strip()),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["supabase"].get("ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())
