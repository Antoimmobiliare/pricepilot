"""
PricePilot - Tenant/account context.

Local development runs as account 1. In production, API keys or auth middleware
must resolve the account server-side. Client supplied account_id values are not
trusted for tenant isolation.
"""
from __future__ import annotations

import json
import os
from typing import Dict, Optional

API_KEY_HEADER = "X-PricePilot-API-Key"


def default_account_id() -> int:
    try:
        return max(1, int(os.getenv("PRICEPILOT_DEFAULT_ACCOUNT_ID", "1")))
    except ValueError:
        return 1


def configured_api_keys() -> Dict[str, int]:
    """
    Returns API key -> account_id mappings.

    Supported envs:
      PRICEPILOT_API_KEYS_JSON='{"key-a": 1, "key-b": 2}'
      PRICEPILOT_API_KEY='single-local-key' + PRICEPILOT_DEFAULT_ACCOUNT_ID
    """
    raw = os.getenv("PRICEPILOT_API_KEYS_JSON", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            return {
                str(key): max(1, int(value))
                for key, value in parsed.items()
                if str(key).strip()
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    legacy_key = os.getenv("PRICEPILOT_API_KEY", "").strip()
    if legacy_key:
        return {legacy_key: default_account_id()}
    return {}


def production_mode() -> bool:
    env = os.getenv("PRICEPILOT_ENV", "").strip().lower()
    explicit = os.getenv("PRICEPILOT_API_AUTH_REQUIRED", "").strip().lower()
    return env in {"prod", "production"} or explicit in {"1", "true", "yes", "on"}


def api_auth_required() -> bool:
    return production_mode() or bool(configured_api_keys())


def resolve_account_id_from_api_key(api_key: str | None) -> Optional[int]:
    keys = configured_api_keys()
    if not keys:
        return None if production_mode() else default_account_id()
    return keys.get(api_key or "")
