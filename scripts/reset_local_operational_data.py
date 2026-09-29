"""Delete only pre-audited local PricePilot test data before Luma setup.

Fixtures and providers under tests/ and pricepilot/ are never touched. This script
does not contact Supabase, Beds24, Telegram or any OTA.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "data" / "pricepilot.db"

AUDITED_TEST_ACCOUNTS = {
    1: "La mia attivita", 2: "antoniobeb", 3: "Antonioss", 4: "totos",
    5: "anto", 6: "ciro", 7: "ciro", 8: "bene", 9: "anna",
    10: "Antobeb", 11: "antopisa",
}


def _placeholders(values: tuple[int, ...]) -> str:
    return ",".join("?" * len(values))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-reset", action="store_true", help="required: remove the pre-audited test accounts only")
    args = parser.parse_args()
    if not args.confirm_reset:
        parser.error("Refusing to remove data without --confirm-reset.")
    if not DATABASE.exists():
        print("Local database not found; nothing to reset.")
        return 0

    account_ids = tuple(AUDITED_TEST_ACCOUNTS)
    account_marks = _placeholders(account_ids)
    with sqlite3.connect(DATABASE) as conn:
        actual_accounts = dict(conn.execute(
            f"SELECT id, name FROM accounts WHERE id IN ({account_marks})", account_ids
        ))
        if actual_accounts != AUDITED_TEST_ACCOUNTS:
            raise RuntimeError("Audit changed: refusing to remove accounts whose IDs or names no longer match the verified test list.")
        property_ids = tuple(row[0] for row in conn.execute(
            f"SELECT id FROM properties WHERE account_id IN ({account_marks})", account_ids
        ))
        user_ids = tuple(row[0] for row in conn.execute(
            f"SELECT id FROM users WHERE account_id IN ({account_marks})", account_ids
        ))
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        removed = 0
        try:
            for (name,) in tables:
                if name in {"accounts", "properties", "users"}:
                    continue
                safe_name = name.replace('"', '""')
                columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{safe_name}")')}
                if "account_id" in columns:
                    removed += conn.execute(f'DELETE FROM "{safe_name}" WHERE account_id IN ({account_marks})', account_ids).rowcount
                elif property_ids and "property_id" in columns:
                    property_marks = _placeholders(property_ids)
                    removed += conn.execute(f'DELETE FROM "{safe_name}" WHERE property_id IN ({property_marks})', property_ids).rowcount
                elif user_ids and "user_id" in columns:
                    user_marks = _placeholders(user_ids)
                    removed += conn.execute(f'DELETE FROM "{safe_name}" WHERE user_id IN ({user_marks})', user_ids).rowcount
            if property_ids:
                property_marks = _placeholders(property_ids)
                removed += conn.execute(f'DELETE FROM properties WHERE id IN ({property_marks})', property_ids).rowcount
            if user_ids:
                user_marks = _placeholders(user_ids)
                removed += conn.execute(f'DELETE FROM users WHERE id IN ({user_marks})', user_ids).rowcount
            removed += conn.execute(f'DELETE FROM accounts WHERE id IN ({account_marks})', account_ids).rowcount
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"Foreign key violations after reset: {violations!r}")
        except Exception:
            conn.rollback()
            raise
    print(f"Pre-audited local PricePilot test data removed: {removed} rows for {len(account_ids)} accounts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
