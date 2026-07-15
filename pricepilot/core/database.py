"""
PricePilot - Database Manager
Gestione SQLite con schema completo per storico decisioni,
competitor, eventi e snapshot di mercato.
"""
import hashlib
import secrets
import sqlite3
import json
from functools import wraps
from datetime import datetime, date, timedelta
from pathlib import Path
from contextlib import contextmanager
from typing import List, Optional, Dict, Any

from pricepilot.core.config import CONFIG
from pricepilot.core.data_backend import (
    CloudDatabaseUnavailable,
    is_supabase_primary,
    require_supabase_primary,
)


def get_db_path() -> str:
    path = Path(CONFIG["db_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


@contextmanager
def get_conn():
    if is_supabase_primary():
        raise CloudDatabaseUnavailable(
            "SQLite locale non disponibile: PRICEPILOT_DATABASE_BACKEND=supabase."
        )
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Crea tutte le tabelle se non esistono."""
    if is_supabase_primary():
        # Lo schema cloud viene gestito dalle migrazioni Supabase versionate.
        require_supabase_primary(require_service_role=True)
        return
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS properties (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id  INTEGER NOT NULL DEFAULT 1,
            name        TEXT    NOT NULL,
            platform    TEXT    NOT NULL DEFAULT 'airbnb',
            listing_url TEXT,
            listing_id  TEXT,
            city        TEXT,
            latitude    REAL,
            longitude   REAL,
            min_price   REAL    NOT NULL DEFAULT 50.0,
            max_price   REAL    NOT NULL DEFAULT 500.0,
            sync_mode   TEXT    NOT NULL DEFAULT 'advisory',
            strategy    TEXT    NOT NULL DEFAULT 'balanced',
            plan        TEXT    NOT NULL DEFAULT 'free',
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS accounts (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT    NOT NULL,
            plan           TEXT    NOT NULL DEFAULT 'free',
            billing_status TEXT    NOT NULL DEFAULT 'dev',
            trial_ends_at   TEXT,
            current_period_ends_at TEXT,
            stripe_customer_id TEXT DEFAULT '',
            stripe_subscription_id TEXT DEFAULT '',
            created_at     TEXT    NOT NULL,
            updated_at     TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL DEFAULT 1,
            full_name  TEXT    DEFAULT '',
            email      TEXT    NOT NULL UNIQUE,
            role       TEXT    NOT NULL DEFAULT 'owner',
            password_hash TEXT DEFAULT '',
            auth_provider TEXT DEFAULT 'local',
            external_user_id TEXT DEFAULT '',
            last_login_at TEXT,
            created_at TEXT    NOT NULL,
            updated_at TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS auth_sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            token_hash  TEXT    NOT NULL UNIQUE,
            expires_at  TEXT    NOT NULL,
            revoked_at  TEXT,
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_auth_sessions_token
            ON auth_sessions(token_hash);
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
            ON auth_sessions(user_id, expires_at);

        CREATE TABLE IF NOT EXISTS user_consents (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id         INTEGER NOT NULL,
            user_id            INTEGER NOT NULL,
            terms_accepted     INTEGER NOT NULL DEFAULT 0,
            privacy_accepted   INTEGER NOT NULL DEFAULT 0,
            marketing_accepted INTEGER NOT NULL DEFAULT 0,
            terms_version      TEXT    NOT NULL DEFAULT '2026-07-13',
            privacy_version    TEXT    NOT NULL DEFAULT '2026-07-13',
            source             TEXT    NOT NULL DEFAULT 'signup',
            accepted_at        TEXT    NOT NULL,
            created_at         TEXT    NOT NULL,
            updated_at         TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS decision_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,
            account_id  INTEGER NOT NULL DEFAULT 1,
            property_id INTEGER NOT NULL DEFAULT 1,
            old_price   REAL    NOT NULL,
            new_price   REAL    NOT NULL,
            market_avg  REAL,
            occupancy   REAL,
            decision    TEXT,
            mode        TEXT    NOT NULL DEFAULT 'advisory',
            applied     INTEGER DEFAULT 0,
            current_price_source TEXT DEFAULT 'manual',
            data_source TEXT DEFAULT 'demo',
            notes       TEXT
        );

        CREATE TABLE IF NOT EXISTS occupancy_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id  INTEGER NOT NULL DEFAULT 1,
            property_id INTEGER NOT NULL DEFAULT 1,
            date        TEXT    NOT NULL,
            occupancy   REAL    NOT NULL,
            source      TEXT    DEFAULT 'manual',
            UNIQUE(property_id, date)
        );

        CREATE TABLE IF NOT EXISTS market_history (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id       INTEGER NOT NULL DEFAULT 1,
            property_id      INTEGER NOT NULL DEFAULT 1,
            date             TEXT    NOT NULL,
            market_avg       REAL,
            market_min       REAL,
            market_max       REAL,
            market_std       REAL,
            competitor_count INTEGER,
            source           TEXT DEFAULT 'demo',
            recorded_at      TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS price_calendar (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id           INTEGER NOT NULL DEFAULT 1,
            property_id          INTEGER NOT NULL,
            date                 TEXT    NOT NULL,
            current_price        REAL    NOT NULL,
            current_price_source TEXT    NOT NULL DEFAULT 'manual',
            recommended_price    REAL,
            status               TEXT    NOT NULL DEFAULT 'current',
            decision_log_id      INTEGER,
            applied_price        REAL,
            notes                TEXT    DEFAULT '',
            created_at           TEXT    NOT NULL,
            updated_at           TEXT    NOT NULL,
            UNIQUE(account_id, property_id, date)
        );

        CREATE INDEX IF NOT EXISTS idx_decision_log_ts
            ON decision_log(timestamp);
        CREATE INDEX IF NOT EXISTS idx_occupancy_date
            ON occupancy_history(date);
        CREATE INDEX IF NOT EXISTS idx_market_history_date
            ON market_history(date);
        CREATE INDEX IF NOT EXISTS idx_price_calendar_lookup
            ON price_calendar(account_id, property_id, date);

        CREATE TABLE IF NOT EXISTS pricing_decisions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id       INTEGER NOT NULL DEFAULT 1,
            timestamp        TEXT    NOT NULL,
            date             TEXT    NOT NULL,
            property_id      TEXT    NOT NULL DEFAULT 'default',
            old_price        REAL    NOT NULL,
            new_price        REAL    NOT NULL,
            pct_change       REAL    NOT NULL,
            competitor_price REAL,
            market_price     REAL,
            competitor_count INTEGER,
            competitor_min   REAL,
            competitor_max   REAL,
            occupancy        REAL,
            event            TEXT,
            strategy         TEXT,
            decision         TEXT,
            applied          INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS competitors (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT    NOT NULL,
            date            TEXT    NOT NULL,
            source          TEXT    NOT NULL,
            property_name   TEXT,
            price           REAL    NOT NULL,
            occupancy_rate  REAL,
            rating          REAL,
            num_reviews     INTEGER
        );

        CREATE TABLE IF NOT EXISTS events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            date         TEXT    NOT NULL,
            name         TEXT    NOT NULL,
            event_type   TEXT,
            impact_level TEXT,
            description  TEXT,
            UNIQUE(date, name)
        );

        CREATE TABLE IF NOT EXISTS market_snapshots (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp        TEXT    NOT NULL,
            date             TEXT    NOT NULL,
            market_avg       REAL,
            market_min       REAL,
            market_max       REAL,
            competitor_count INTEGER,
            our_price        REAL,
            position         TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_decisions_date
            ON pricing_decisions(date);
        CREATE INDEX IF NOT EXISTS idx_decisions_ts
            ON pricing_decisions(timestamp);
        CREATE INDEX IF NOT EXISTS idx_competitors_date
            ON competitors(date);
        CREATE INDEX IF NOT EXISTS idx_snapshots_date
            ON market_snapshots(date);

        CREATE TABLE IF NOT EXISTS telegram_links (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            property_id         INTEGER NOT NULL,
            token               TEXT    NOT NULL UNIQUE,
            chat_id             INTEGER,
            telegram_username   TEXT    DEFAULT '',
            active              INTEGER DEFAULT 1,
            created_at          TEXT    NOT NULL,
            updated_at          TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_telegram_token
            ON telegram_links(token);
        CREATE INDEX IF NOT EXISTS idx_telegram_property
            ON telegram_links(property_id);

        CREATE TABLE IF NOT EXISTS telegram_approvals (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           TEXT    NOT NULL,
            account_id          INTEGER NOT NULL DEFAULT 1,
            property_id         INTEGER,
            decision_log_id     INTEGER NOT NULL,
            telegram_link_id    INTEGER,
            chat_id             INTEGER,
            telegram_username   TEXT    DEFAULT '',
            action              TEXT    NOT NULL,
            status              TEXT    NOT NULL,
            source              TEXT    NOT NULL DEFAULT 'telegram',
            message_id          TEXT    DEFAULT '',
            callback_query_id   TEXT    DEFAULT '',
            error               TEXT    DEFAULT '',
            payload             TEXT    DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_telegram_approvals_account
            ON telegram_approvals(account_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_telegram_approvals_decision
            ON telegram_approvals(decision_log_id);

        CREATE TABLE IF NOT EXISTS property_integrations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            property_id INTEGER NOT NULL,
            platform    TEXT    NOT NULL,
            listing_url TEXT    DEFAULT '',
            listing_id  TEXT    DEFAULT '',
            is_primary  INTEGER DEFAULT 0,
            created_at  TEXT    NOT NULL,
            UNIQUE(property_id, platform)
        );

        CREATE INDEX IF NOT EXISTS idx_prop_integrations_pid
            ON property_integrations(property_id);

        CREATE TABLE IF NOT EXISTS guardrail_policies (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id             INTEGER NOT NULL DEFAULT 1,
            property_id            INTEGER NOT NULL DEFAULT 0,
            max_change_pct         REAL    NOT NULL DEFAULT 0.20,
            require_approval_pct   REAL    NOT NULL DEFAULT 0.15,
            min_confidence_auto    REAL    NOT NULL DEFAULT 0.80,
            competitor_outlier_pct REAL    NOT NULL DEFAULT 0.60,
            max_daily_auto_changes INTEGER NOT NULL DEFAULT 4,
            auto_enabled           INTEGER NOT NULL DEFAULT 1,
            created_at             TEXT    NOT NULL,
            updated_at             TEXT    NOT NULL,
            UNIQUE(account_id, property_id)
        );

        CREATE TABLE IF NOT EXISTS operation_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id      INTEGER NOT NULL DEFAULT 1,
            source          TEXT    NOT NULL DEFAULT 'scheduler',
            status          TEXT    NOT NULL DEFAULT 'running',
            started_at      TEXT    NOT NULL,
            finished_at     TEXT,
            next_run_at     TEXT,
            decisions_count INTEGER NOT NULL DEFAULT 0,
            summary         TEXT    DEFAULT '{}',
            error           TEXT    DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS audit_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,
            account_id  INTEGER NOT NULL DEFAULT 1,
            property_id INTEGER,
            source      TEXT    NOT NULL DEFAULT 'system',
            action      TEXT    NOT NULL,
            entity_type TEXT    NOT NULL DEFAULT 'system',
            entity_id   TEXT    DEFAULT '',
            status      TEXT    NOT NULL DEFAULT 'ok',
            details     TEXT    DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_operation_runs_account
            ON operation_runs(account_id, started_at);
        CREATE INDEX IF NOT EXISTS idx_audit_events_account
            ON audit_events(account_id, timestamp);

        CREATE TABLE IF NOT EXISTS notification_preferences (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id       INTEGER NOT NULL DEFAULT 1,
            property_id      INTEGER NOT NULL DEFAULT 0,
            telegram_enabled INTEGER NOT NULL DEFAULT 1,
            quiet_hours_start TEXT DEFAULT '',
            quiet_hours_end   TEXT DEFAULT '',
            daily_digest     INTEGER NOT NULL DEFAULT 1,
            approval_alerts  INTEGER NOT NULL DEFAULT 1,
            auto_reports     INTEGER NOT NULL DEFAULT 1,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            UNIQUE(account_id, property_id)
        );

        CREATE TABLE IF NOT EXISTS notification_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT NOT NULL,
            account_id  INTEGER NOT NULL DEFAULT 1,
            property_id INTEGER,
            channel     TEXT NOT NULL DEFAULT 'telegram',
            event_type  TEXT NOT NULL,
            recipient   TEXT DEFAULT '',
            status      TEXT NOT NULL,
            message_id  TEXT DEFAULT '',
            error       TEXT DEFAULT '',
            payload     TEXT DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_notification_log_account
            ON notification_log(account_id, timestamp);

        CREATE INDEX IF NOT EXISTS idx_user_consents_user
            ON user_consents(user_id, accepted_at);
        """)

    # ── Migrazioni sicure (ADD COLUMN se colonna mancante) ────────────────────
    with get_conn() as conn:
        # --- accounts ---
        cols_a = {r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        for _col, _typ in [
            ("trial_ends_at", "TEXT"),
            ("current_period_ends_at", "TEXT"),
            ("stripe_customer_id", "TEXT DEFAULT ''"),
            ("stripe_subscription_id", "TEXT DEFAULT ''"),
        ]:
            if _col not in cols_a:
                conn.execute(f"ALTER TABLE accounts ADD COLUMN {_col} {_typ}")

        # --- users ---
        cols_u = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "full_name" not in cols_u:
            conn.execute("ALTER TABLE users ADD COLUMN full_name TEXT DEFAULT ''")
        for _col, _typ in [
            ("password_hash", "TEXT DEFAULT ''"),
            ("auth_provider", "TEXT DEFAULT 'local'"),
            ("external_user_id", "TEXT DEFAULT ''"),
            ("last_login_at", "TEXT"),
        ]:
            if _col not in cols_u:
                conn.execute(f"ALTER TABLE users ADD COLUMN {_col} {_typ}")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_consents (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id         INTEGER NOT NULL,
                user_id            INTEGER NOT NULL,
                terms_accepted     INTEGER NOT NULL DEFAULT 0,
                privacy_accepted   INTEGER NOT NULL DEFAULT 0,
                marketing_accepted INTEGER NOT NULL DEFAULT 0,
                terms_version      TEXT    NOT NULL DEFAULT '2026-07-13',
                privacy_version    TEXT    NOT NULL DEFAULT '2026-07-13',
                source             TEXT    NOT NULL DEFAULT 'signup',
                accepted_at        TEXT    NOT NULL,
                created_at         TEXT    NOT NULL,
                updated_at         TEXT    NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_consents_user "
            "ON user_consents(user_id, accepted_at)"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_approvals (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp           TEXT    NOT NULL,
                account_id          INTEGER NOT NULL DEFAULT 1,
                property_id         INTEGER,
                decision_log_id     INTEGER NOT NULL,
                telegram_link_id    INTEGER,
                chat_id             INTEGER,
                telegram_username   TEXT    DEFAULT '',
                action              TEXT    NOT NULL,
                status              TEXT    NOT NULL,
                source              TEXT    NOT NULL DEFAULT 'telegram',
                message_id          TEXT    DEFAULT '',
                callback_query_id   TEXT    DEFAULT '',
                error               TEXT    DEFAULT '',
                payload             TEXT    DEFAULT '{}'
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telegram_approvals_account "
            "ON telegram_approvals(account_id, timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telegram_approvals_decision "
            "ON telegram_approvals(decision_log_id)"
        )

        # --- properties ---
        cols_p = {r[1] for r in conn.execute("PRAGMA table_info(properties)").fetchall()}
        if "account_id" not in cols_p:
            conn.execute(
                "ALTER TABLE properties ADD COLUMN account_id INTEGER NOT NULL DEFAULT 1"
            )
        if "strategy" not in cols_p:
            conn.execute(
                "ALTER TABLE properties ADD COLUMN strategy TEXT NOT NULL DEFAULT 'balanced'"
            )
        if "plan" not in cols_p:
            conn.execute(
                "ALTER TABLE properties ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'"
            )

        ensure_default_account(conn)
        conn.execute(
            "UPDATE accounts SET name='La mia attivita' WHERE id=1 AND name='Local Dev Account'"
        )
        ensure_default_guardrail_policy(conn=conn)
        ensure_default_notification_preferences(conn=conn)
        conn.execute("UPDATE properties SET account_id=1 WHERE account_id IS NULL")

        # --- pricing_decisions legacy table ---
        cols_pd = {r[1] for r in conn.execute("PRAGMA table_info(pricing_decisions)").fetchall()}
        if "account_id" not in cols_pd:
            conn.execute("ALTER TABLE pricing_decisions ADD COLUMN account_id INTEGER NOT NULL DEFAULT 1")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_decisions_account "
            "ON pricing_decisions(account_id, timestamp)"
        )

        # --- decision_log ---
        cols = {row[1] for row in conn.execute("PRAGMA table_info(decision_log)").fetchall()}
        if "reason" not in cols:
            conn.execute("ALTER TABLE decision_log ADD COLUMN reason TEXT DEFAULT ''")
        if "tg_message_id" not in cols:
            conn.execute("ALTER TABLE decision_log ADD COLUMN tg_message_id INTEGER")
        # Nuovi campi data storage improvement
        for _col, _typ in [
            ("account_id",     "INTEGER NOT NULL DEFAULT 1"),
            ("date",          "TEXT"),
            ("competitor_avg","REAL"),
            ("strategy",      "TEXT"),
            ("factors",       "TEXT"),
            ("mpi",           "REAL"),
            ("current_price_source", "TEXT DEFAULT 'manual'"),
            ("data_source",    "TEXT DEFAULT 'demo'"),
        ]:
            if _col not in cols:
                conn.execute(
                    f"ALTER TABLE decision_log ADD COLUMN {_col} {_typ}"
                )

        # --- occupancy_history ---
        cols_occ = {row[1] for row in conn.execute("PRAGMA table_info(occupancy_history)").fetchall()}
        if "account_id" not in cols_occ:
            conn.execute("ALTER TABLE occupancy_history ADD COLUMN account_id INTEGER NOT NULL DEFAULT 1")

        # --- market_history ---
        cols_mkt = {row[1] for row in conn.execute("PRAGMA table_info(market_history)").fetchall()}
        if "account_id" not in cols_mkt:
            conn.execute("ALTER TABLE market_history ADD COLUMN account_id INTEGER NOT NULL DEFAULT 1")
        if "source" not in cols_mkt:
            conn.execute("ALTER TABLE market_history ADD COLUMN source TEXT DEFAULT 'demo'")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_decision_log_account "
            "ON decision_log(account_id, timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_occupancy_account "
            "ON occupancy_history(account_id, property_id, date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_history_account "
            "ON market_history(account_id, property_id, date)"
        )


# ─────────────────────────────────────────────
# PRICING DECISIONS
# ─────────────────────────────────────────────

def save_decision(decision: Dict[str, Any]) -> int:
    """Salva una decisione di pricing. Ritorna l'id inserito."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO pricing_decisions
                (account_id, timestamp, date, property_id, old_price, new_price, pct_change,
                 competitor_price, market_price, competitor_count, competitor_min,
                 competitor_max, occupancy, event, strategy, decision, applied)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            decision.get("account_id", 1),
            decision.get("timestamp", now),
            decision["date"],
            decision.get("property_id", CONFIG.get("property_id", "default")),
            decision["old_price"],
            decision["new_price"],
            decision["pct_change"],
            decision.get("competitor_price"),
            decision.get("market_price"),
            decision.get("competitor_count"),
            decision.get("competitor_min"),
            decision.get("competitor_max"),
            decision.get("occupancy"),
            decision.get("event", ""),
            decision.get("strategy", CONFIG.get("strategy", "balanced")),
            decision.get("decision", ""),
            int(decision.get("applied", 0)),
        ))
        return cur.lastrowid


def get_decisions(
    limit: int = 200,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    account_id: Optional[int] = None,
) -> List[Dict]:
    query = "SELECT * FROM pricing_decisions WHERE 1=1"
    params: list = []
    if account_id is not None:
        query += " AND account_id=?"
        params.append(account_id)
    if date_from:
        query += " AND date >= ?"
        params.append(date_from)
    if date_to:
        query += " AND date <= ?"
        params.append(date_to)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# COMPETITORS
# ─────────────────────────────────────────────

def save_competitors(competitors: List[Dict]) -> None:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO competitors
                (timestamp, date, source, property_name, price,
                 occupancy_rate, rating, num_reviews)
            VALUES (?,?,?,?,?,?,?,?)
        """, [(
            now,
            c["date"],
            c.get("source", "unknown"),
            c.get("property_name", ""),
            c["price"],
            c.get("occupancy_rate"),
            c.get("rating"),
            c.get("num_reviews"),
        ) for c in competitors])


def get_competitors(target_date: str) -> List[Dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM competitors WHERE date=? ORDER BY price",
            (target_date,)
        ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# EVENTS
# ─────────────────────────────────────────────

def upsert_event(evt: Dict) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO events
                (date, name, event_type, impact_level, description)
            VALUES (?,?,?,?,?)
        """, (
            evt["date"],
            evt["name"],
            evt.get("event_type", "generic"),
            evt.get("impact_level", "medium"),
            evt.get("description", ""),
        ))


def get_events(date_from: Optional[str] = None, date_to: Optional[str] = None) -> List[Dict]:
    query = "SELECT * FROM events WHERE 1=1"
    params: list = []
    if date_from:
        query += " AND date >= ?"
        params.append(date_from)
    if date_to:
        query += " AND date <= ?"
        params.append(date_to)
    query += " ORDER BY date"
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# MARKET SNAPSHOTS
# ─────────────────────────────────────────────

def save_market_snapshot(snap: Dict) -> None:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO market_snapshots
                (timestamp, date, market_avg, market_min, market_max,
                 competitor_count, our_price, position)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            now,
            snap["date"],
            snap.get("market_avg"),
            snap.get("market_min"),
            snap.get("market_max"),
            snap.get("competitor_count"),
            snap.get("our_price"),
            snap.get("position", ""),
        ))


def get_market_snapshots(limit: int = 90) -> List[Dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM market_snapshots ORDER BY date DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# STATISTICHE
# ─────────────────────────────────────────────

def get_summary_stats(account_id: Optional[int] = None) -> Dict[str, Any]:
    """Restituisce statistiche aggregate per la dashboard."""
    where = ""
    params: list = []
    if account_id is not None:
        where = " WHERE account_id=?"
        params.append(account_id)
    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM pricing_decisions{where}",
            params,
        ).fetchone()[0]
        avg_price = conn.execute(
            f"SELECT AVG(new_price) FROM pricing_decisions{where}",
            params,
        ).fetchone()[0]
        last_decision = conn.execute(
            f"SELECT * FROM pricing_decisions{where} ORDER BY timestamp DESC LIMIT 1",
            params,
        ).fetchone()
        avg_change = conn.execute(
            f"SELECT AVG(ABS(pct_change)) FROM pricing_decisions{where}",
            params,
        ).fetchone()[0]
    return {
        "total_decisions": total,
        "avg_price": round(avg_price or 0, 2),
        "avg_change_pct": round((avg_change or 0) * 100, 2),
        "last_decision": dict(last_decision) if last_decision else None,
    }


# ─────────────────────────────────────────────
# PROPERTIES
# ─────────────────────────────────────────────

def ensure_default_account(conn=None) -> int:
    """Crea l'account locale di sviluppo se manca. Ritorna account_id."""
    now = datetime.utcnow().isoformat()
    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(get_db_path())
        conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT id FROM accounts WHERE id=1").fetchone()
        if not row:
            conn.execute("""
                INSERT INTO accounts
                    (id, name, plan, billing_status, created_at, updated_at)
                VALUES (1, ?, 'free', 'dev', ?, ?)
            """, ("Local Dev Account", now, now))
        user = conn.execute("SELECT id FROM users WHERE email=?", ("local@pricepilot.dev",)).fetchone()
        if not user:
            conn.execute("""
                INSERT INTO users
                    (account_id, email, role, created_at, updated_at)
                VALUES (1, 'local@pricepilot.dev', 'owner', ?, ?)
            """, (now, now))
        if owns_conn:
            conn.commit()
        return 1
    finally:
        if owns_conn:
            conn.close()


def create_account(
    name: str,
    plan: str = "free",
    billing_status: str = "dev",
) -> Dict:
    """Crea un account SaaS e ritorna la riga creata."""
    now = datetime.utcnow().isoformat()
    clean_name = (name or "La mia attivita").strip() or "La mia attivita"
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO accounts
                (name, plan, billing_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (clean_name, plan or "free", billing_status or "dev", now, now))
        account_id = cur.lastrowid
    ensure_default_guardrail_policy(account_id)
    ensure_default_notification_preferences(account_id)
    return get_account(account_id) or {}


def get_account(account_id: int = 1) -> Optional[Dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    return dict(row) if row else None


def update_account(account_id: int, data: Dict) -> Optional[Dict]:
    now = datetime.utcnow().isoformat()
    existing = get_account(account_id)
    if not existing:
        return None
    merged = {**existing, **data}
    with get_conn() as conn:
        conn.execute("""
            UPDATE accounts
               SET name=?, plan=?, billing_status=?, trial_ends_at=?,
                   current_period_ends_at=?, stripe_customer_id=?,
                   stripe_subscription_id=?, updated_at=?
             WHERE id=?
        """, (
            merged.get("name", existing["name"]),
            merged.get("plan", existing["plan"]),
            merged.get("billing_status", existing["billing_status"]),
            merged.get("trial_ends_at", existing.get("trial_ends_at")),
            merged.get("current_period_ends_at", existing.get("current_period_ends_at")),
            merged.get("stripe_customer_id", existing.get("stripe_customer_id", "")),
            merged.get("stripe_subscription_id", existing.get("stripe_subscription_id", "")),
            now,
            account_id,
        ))
    return get_account(account_id)


def get_users(account_id: Optional[int] = None) -> List[Dict]:
    query = "SELECT * FROM users WHERE 1=1"
    params: list = []
    if account_id is not None:
        query += " AND account_id=?"
        params.append(account_id)
    query += " ORDER BY id"
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_user(user_id: int) -> Optional[Dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_email(email: str) -> Optional[Dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE lower(email)=lower(?)",
            ((email or "").strip(),),
        ).fetchone()
    return dict(row) if row else None


def create_user(account_id: int, email: str, role: str = "manager", full_name: str = "") -> Dict:
    now = datetime.utcnow().isoformat()
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("Email utente obbligatoria.")
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO users
                (account_id, full_name, email, role, password_hash,
                 auth_provider, external_user_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            account_id,
            full_name.strip(),
            email,
            role,
            "",
            "local",
            "",
            now,
            now,
        ))
        user_id = cur.lastrowid
    return get_user(user_id)


def update_user(user_id: int, data: Dict) -> Optional[Dict]:
    existing = get_user(user_id)
    if not existing:
        return None
    now = datetime.utcnow().isoformat()
    merged = {**existing, **data}
    with get_conn() as conn:
        conn.execute("""
            UPDATE users
               SET account_id=?, full_name=?, email=?, role=?,
                   password_hash=?, auth_provider=?, external_user_id=?,
                   last_login_at=?, updated_at=?
             WHERE id=?
        """, (
            int(merged.get("account_id") or existing["account_id"]),
            (merged.get("full_name") or "").strip(),
            (merged.get("email") or existing["email"]).strip().lower(),
            merged.get("role") or existing["role"],
            merged.get("password_hash", existing.get("password_hash", "")),
            merged.get("auth_provider", existing.get("auth_provider", "local")),
            merged.get("external_user_id", existing.get("external_user_id", "")),
            merged.get("last_login_at", existing.get("last_login_at")),
            now,
            user_id,
        ))
    return get_user(user_id)


def _auth_session_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def create_auth_session(user_id: int, ttl_days: int = 14) -> str:
    """Crea una sessione persistente locale e ritorna il token raw per il cookie."""
    user_id = int(user_id or 0)
    if user_id <= 0:
        raise ValueError("User id non valido per la sessione.")
    token = secrets.token_urlsafe(40)
    now = datetime.utcnow()
    expires_at = (now + timedelta(days=max(1, int(ttl_days or 14)))).isoformat()
    now_iso = now.isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO auth_sessions
                (user_id, token_hash, expires_at, revoked_at, created_at, updated_at)
            VALUES (?, ?, ?, NULL, ?, ?)
            """,
            (user_id, _auth_session_hash(token), expires_at, now_iso, now_iso),
        )
    return token


def get_user_by_auth_session(token: str) -> Optional[Dict]:
    """Ritorna l'utente collegato a un cookie sessione ancora valido."""
    token = (token or "").strip()
    if not token:
        return None
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT u.*
              FROM auth_sessions s
              JOIN users u ON u.id = s.user_id
             WHERE s.token_hash=?
               AND s.revoked_at IS NULL
               AND s.expires_at > ?
             LIMIT 1
            """,
            (_auth_session_hash(token), now),
        ).fetchone()
    return dict(row) if row else None


def revoke_auth_session(token: str) -> None:
    """Revoca una sessione persistente locale."""
    token = (token or "").strip()
    if not token:
        return
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE auth_sessions
               SET revoked_at=?, updated_at=?
             WHERE token_hash=? AND revoked_at IS NULL
            """,
            (now, now, _auth_session_hash(token)),
        )


def record_user_consent(
    user_id: int,
    account_id: int,
    *,
    terms_accepted: bool,
    privacy_accepted: bool,
    marketing_accepted: bool = False,
    terms_version: str = "2026-07-13",
    privacy_version: str = "2026-07-13",
    source: str = "signup",
    accepted_at: str | None = None,
) -> Dict:
    """Registra un'accettazione privacy/termini per audit SaaS."""
    if not user_id:
        raise ValueError("user_id obbligatorio per registrare il consenso.")
    if not account_id:
        raise ValueError("account_id obbligatorio per registrare il consenso.")

    now = datetime.utcnow().isoformat()
    accepted_at = accepted_at or now
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO user_consents
                (account_id, user_id, terms_accepted, privacy_accepted,
                 marketing_accepted, terms_version, privacy_version, source,
                 accepted_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            int(account_id),
            int(user_id),
            1 if terms_accepted else 0,
            1 if privacy_accepted else 0,
            1 if marketing_accepted else 0,
            terms_version or "2026-07-13",
            privacy_version or "2026-07-13",
            source or "signup",
            accepted_at,
            now,
            now,
        ))
        consent_id = cur.lastrowid
        row = conn.execute(
            "SELECT * FROM user_consents WHERE id=?",
            (consent_id,),
        ).fetchone()
    return dict(row) if row else {}


def get_latest_user_consent(user_id: int) -> Optional[Dict]:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM user_consents
             WHERE user_id=?
             ORDER BY accepted_at DESC, id DESC
             LIMIT 1
            """,
            (int(user_id),),
        ).fetchone()
    return dict(row) if row else None


def delete_user(user_id: int) -> bool:
    user = get_user(user_id)
    if not user:
        return False
    owners = [
        u for u in get_users(int(user.get("account_id") or 1))
        if u.get("role") == "owner" and int(u.get("id")) != int(user_id)
    ]
    if user.get("role") == "owner" and not owners:
        raise ValueError("Non puoi eliminare l'ultimo owner dell'account.")
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    return True


def get_effective_plan_for_property(prop: Dict) -> str:
    """Piano account-first, con fallback al piano legacy salvato sulla proprieta."""
    account = get_account(int(prop.get("account_id") or 1))
    if account:
        status = str(account.get("billing_status") or "dev").lower()
        if status not in {"active", "trialing", "dev"}:
            return "free"
        return account.get("plan") or prop.get("plan", "free")
    return prop.get("plan", "free")


DEFAULT_GUARDRAIL_POLICY = {
    "max_change_pct": 0.20,
    "require_approval_pct": 0.15,
    "min_confidence_auto": 0.80,
    "competitor_outlier_pct": 0.60,
    "max_daily_auto_changes": 4,
    "auto_enabled": 1,
}


def ensure_default_guardrail_policy(account_id: int = 1, conn=None) -> int:
    """Crea la policy guardrail di account se manca. property_id=0 = default account."""
    now = datetime.utcnow().isoformat()
    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(get_db_path())
        conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id FROM guardrail_policies WHERE account_id=? AND property_id=0",
            (account_id,),
        ).fetchone()
        if row:
            return int(row["id"] if hasattr(row, "keys") else row[0])
        cur = conn.execute("""
            INSERT INTO guardrail_policies
                (account_id, property_id, max_change_pct, require_approval_pct,
                 min_confidence_auto, competitor_outlier_pct, max_daily_auto_changes,
                 auto_enabled, created_at, updated_at)
            VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            account_id,
            DEFAULT_GUARDRAIL_POLICY["max_change_pct"],
            DEFAULT_GUARDRAIL_POLICY["require_approval_pct"],
            DEFAULT_GUARDRAIL_POLICY["min_confidence_auto"],
            DEFAULT_GUARDRAIL_POLICY["competitor_outlier_pct"],
            DEFAULT_GUARDRAIL_POLICY["max_daily_auto_changes"],
            DEFAULT_GUARDRAIL_POLICY["auto_enabled"],
            now, now,
        ))
        if owns_conn:
            conn.commit()
        return cur.lastrowid
    finally:
        if owns_conn:
            conn.close()


def get_guardrail_policy(account_id: int = 1, property_id: Optional[int] = None) -> Dict:
    """Ritorna la policy guardrail property-specific, o quella default di account."""
    ensure_default_guardrail_policy(account_id)
    with get_conn() as conn:
        row = None
        if property_id is not None:
            row = conn.execute("""
                SELECT * FROM guardrail_policies
                WHERE account_id=? AND property_id=?
            """, (account_id, property_id)).fetchone()
        if not row:
            row = conn.execute("""
                SELECT * FROM guardrail_policies
                WHERE account_id=? AND property_id=0
            """, (account_id,)).fetchone()
    return dict(row) if row else {**DEFAULT_GUARDRAIL_POLICY, "account_id": account_id, "property_id": 0}


def update_guardrail_policy(account_id: int = 1, property_id: int = 0, data: Dict = None) -> Dict:
    """Aggiorna o crea una policy guardrail."""
    data = data or {}
    existing = get_guardrail_policy(account_id, property_id if property_id else None)
    now = datetime.utcnow().isoformat()
    merged = {**DEFAULT_GUARDRAIL_POLICY, **existing, **data}
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO guardrail_policies
                (account_id, property_id, max_change_pct, require_approval_pct,
                 min_confidence_auto, competitor_outlier_pct, max_daily_auto_changes,
                 auto_enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, property_id)
            DO UPDATE SET
                max_change_pct=excluded.max_change_pct,
                require_approval_pct=excluded.require_approval_pct,
                min_confidence_auto=excluded.min_confidence_auto,
                competitor_outlier_pct=excluded.competitor_outlier_pct,
                max_daily_auto_changes=excluded.max_daily_auto_changes,
                auto_enabled=excluded.auto_enabled,
                updated_at=excluded.updated_at
        """, (
            account_id, property_id,
            float(merged.get("max_change_pct", 0.20)),
            float(merged.get("require_approval_pct", 0.15)),
            float(merged.get("min_confidence_auto", 0.80)),
            float(merged.get("competitor_outlier_pct", 0.60)),
            int(merged.get("max_daily_auto_changes", 4)),
            int(merged.get("auto_enabled", 1)),
            existing.get("created_at") or now,
            now,
        ))
    return get_guardrail_policy(account_id, property_id if property_id else None)


def count_auto_actions_today(property_id: int, target_date: Optional[str] = None) -> int:
    """Conta le applicazioni auto live del giorno. Le simulazioni non vengono contate."""
    target_date = target_date or date.today().isoformat()
    with get_conn() as conn:
        row = conn.execute("""
            SELECT COUNT(*)
            FROM decision_log
            WHERE property_id=?
              AND applied=1
              AND decision LIKE 'AUTO_APPLIED%'
              AND COALESCE(date, substr(timestamp, 1, 10))=?
        """, (property_id, target_date)).fetchone()
    return int(row[0] or 0)


def record_audit_event(
    action: str,
    entity_type: str = "system",
    entity_id: Optional[Any] = None,
    account_id: int = 1,
    property_id: Optional[int] = None,
    source: str = "system",
    status: str = "ok",
    details: Optional[Dict] = None,
) -> int:
    """Registra un evento audit append-only per decisioni, run e azioni utente."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO audit_events
                (timestamp, account_id, property_id, source, action,
                 entity_type, entity_id, status, details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now, account_id, property_id, source, action,
            entity_type, "" if entity_id is None else str(entity_id),
            status, json.dumps(details or {}, ensure_ascii=False),
        ))
        return cur.lastrowid


def get_audit_events(
    limit: int = 100,
    account_id: Optional[int] = None,
    property_id: Optional[int] = None,
) -> List[Dict]:
    query = "SELECT * FROM audit_events WHERE 1=1"
    params: list = []
    if account_id is not None:
        query += " AND account_id=?"
        params.append(account_id)
    if property_id is not None:
        query += " AND property_id=?"
        params.append(property_id)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def start_operation_run(
    account_id: int = 1,
    source: str = "scheduler",
    next_run_at: Optional[str] = None,
) -> int:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO operation_runs
                (account_id, source, status, started_at, next_run_at)
            VALUES (?, ?, 'running', ?, ?)
        """, (account_id, source, now, next_run_at))
        return cur.lastrowid


def get_active_operation_run(
    account_id: int = 1,
    stale_after_minutes: int = 120,
) -> Optional[Dict]:
    """
    Ritorna un ciclo ancora running per l'account.
    I running troppo vecchi vengono marcati stale per non bloccare per sempre.
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=max(1, int(stale_after_minutes)))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM operation_runs "
            "WHERE account_id=? AND status='running' "
            "ORDER BY started_at DESC",
            (account_id,),
        ).fetchall()
        active = None
        for row in rows:
            started_raw = str(row["started_at"] or "")
            try:
                started_at = datetime.fromisoformat(started_raw[:19])
            except Exception:
                started_at = now
            if started_at >= cutoff and active is None:
                active = dict(row)
                continue
            conn.execute(
                "UPDATE operation_runs "
                "SET status='stale', finished_at=?, error=? "
                "WHERE id=?",
                (
                    now.isoformat(),
                    "Ciclo rimasto running oltre la soglia: marcato come stale.",
                    row["id"],
                ),
            )
        return active


def try_start_operation_run(
    account_id: int = 1,
    source: str = "scheduler",
    next_run_at: Optional[str] = None,
    stale_after_minutes: int = 120,
) -> tuple[Optional[int], Optional[Dict]]:
    """
    Avvia un ciclo solo se non ne esiste gia uno running.
    Ritorna (run_id, active_run). Se active_run non e None, il caller deve saltare.
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=max(1, int(stale_after_minutes)))
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM operation_runs "
            "WHERE account_id=? AND status='running' "
            "ORDER BY started_at DESC",
            (account_id,),
        ).fetchall()
        active = None
        for row in rows:
            started_raw = str(row["started_at"] or "")
            try:
                started_at = datetime.fromisoformat(started_raw[:19])
            except Exception:
                started_at = now
            if started_at >= cutoff and active is None:
                active = dict(row)
                continue
            conn.execute(
                "UPDATE operation_runs "
                "SET status='stale', finished_at=?, error=? "
                "WHERE id=?",
                (
                    now.isoformat(),
                    "Ciclo rimasto running oltre la soglia: marcato come stale.",
                    row["id"],
                ),
            )
        if active:
            return None, active

        cur = conn.execute("""
            INSERT INTO operation_runs
                (account_id, source, status, started_at, next_run_at)
            VALUES (?, ?, 'running', ?, ?)
        """, (account_id, source, now.isoformat(), next_run_at))
        return cur.lastrowid, None


def finish_operation_run(
    run_id: int,
    status: str,
    decisions_count: int = 0,
    summary: Optional[Dict] = None,
    error: str = "",
    next_run_at: Optional[str] = None,
) -> Optional[Dict]:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute("""
            UPDATE operation_runs
               SET status=?, finished_at=?, decisions_count=?,
                   summary=?, error=?, next_run_at=?
             WHERE id=?
        """, (
            status, now, decisions_count,
            json.dumps(summary or {}, ensure_ascii=False),
            error, next_run_at, run_id,
        ))
    return get_operation_run(run_id)


def get_operation_run(run_id: int) -> Optional[Dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM operation_runs WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def get_operation_runs(limit: int = 50, account_id: Optional[int] = None) -> List[Dict]:
    query = "SELECT * FROM operation_runs WHERE 1=1"
    params: list = []
    if account_id is not None:
        query += " AND account_id=?"
        params.append(account_id)
    query += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_last_operation_run(account_id: int = 1) -> Optional[Dict]:
    runs = get_operation_runs(limit=1, account_id=account_id)
    return runs[0] if runs else None


def ensure_default_notification_preferences(account_id: int = 1, conn=None) -> int:
    """Crea preferenze notifiche default di account se mancano. property_id=0 = default account."""
    now = datetime.utcnow().isoformat()
    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(get_db_path())
        conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id FROM notification_preferences WHERE account_id=? AND property_id=0",
            (account_id,),
        ).fetchone()
        if row:
            return int(row["id"] if hasattr(row, "keys") else row[0])
        cur = conn.execute("""
            INSERT INTO notification_preferences
                (account_id, property_id, telegram_enabled, quiet_hours_start,
                 quiet_hours_end, daily_digest, approval_alerts, auto_reports,
                 created_at, updated_at)
            VALUES (?, 0, 1, '', '', 1, 1, 1, ?, ?)
        """, (account_id, now, now))
        if owns_conn:
            conn.commit()
        return cur.lastrowid
    finally:
        if owns_conn:
            conn.close()


def get_notification_preferences(account_id: int = 1, property_id: Optional[int] = None) -> Dict:
    ensure_default_notification_preferences(account_id)
    with get_conn() as conn:
        row = None
        if property_id is not None:
            row = conn.execute("""
                SELECT * FROM notification_preferences
                WHERE account_id=? AND property_id=?
            """, (account_id, property_id)).fetchone()
        if not row:
            row = conn.execute("""
                SELECT * FROM notification_preferences
                WHERE account_id=? AND property_id=0
            """, (account_id,)).fetchone()
    return dict(row) if row else {
        "account_id": account_id,
        "property_id": property_id or 0,
        "telegram_enabled": 1,
        "daily_digest": 1,
        "approval_alerts": 1,
        "auto_reports": 1,
    }


def update_notification_preferences(account_id: int = 1, property_id: int = 0, data: Dict = None) -> Dict:
    data = data or {}
    existing = get_notification_preferences(account_id, property_id if property_id else None)
    now = datetime.utcnow().isoformat()
    merged = {**existing, **data}
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO notification_preferences
                (account_id, property_id, telegram_enabled, quiet_hours_start,
                 quiet_hours_end, daily_digest, approval_alerts, auto_reports,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, property_id)
            DO UPDATE SET
                telegram_enabled=excluded.telegram_enabled,
                quiet_hours_start=excluded.quiet_hours_start,
                quiet_hours_end=excluded.quiet_hours_end,
                daily_digest=excluded.daily_digest,
                approval_alerts=excluded.approval_alerts,
                auto_reports=excluded.auto_reports,
                updated_at=excluded.updated_at
        """, (
            account_id, property_id,
            int(merged.get("telegram_enabled", 1)),
            merged.get("quiet_hours_start", ""),
            merged.get("quiet_hours_end", ""),
            int(merged.get("daily_digest", 1)),
            int(merged.get("approval_alerts", 1)),
            int(merged.get("auto_reports", 1)),
            existing.get("created_at") or now,
            now,
        ))
    return get_notification_preferences(account_id, property_id if property_id else None)


def record_notification_log(
    event_type: str,
    status: str,
    account_id: int = 1,
    property_id: Optional[int] = None,
    channel: str = "telegram",
    recipient: str = "",
    message_id: str = "",
    error: str = "",
    payload: Optional[Dict] = None,
) -> int:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO notification_log
                (timestamp, account_id, property_id, channel, event_type,
                 recipient, status, message_id, error, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now, account_id, property_id, channel, event_type,
            recipient, status, message_id, error,
            json.dumps(payload or {}, ensure_ascii=False),
        ))
        return cur.lastrowid


def get_notification_log(
    limit: int = 100,
    account_id: Optional[int] = None,
    property_id: Optional[int] = None,
) -> List[Dict]:
    query = "SELECT * FROM notification_log WHERE 1=1"
    params: list = []
    if account_id is not None:
        query += " AND account_id=?"
        params.append(account_id)
    if property_id is not None:
        query += " AND property_id=?"
        params.append(property_id)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def upsert_property(prop: Dict) -> int:
    """Inserisce o aggiorna una proprietà. Ritorna l'id."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM properties WHERE id=?", (prop.get("id"),)
        ).fetchone()
        if existing:
            conn.execute("""
                UPDATE properties SET
                    account_id=?, name=?, platform=?, listing_url=?, listing_id=?,
                    city=?, latitude=?, longitude=?,
                    min_price=?, max_price=?, sync_mode=?, strategy=?, plan=?, updated_at=?
                WHERE id=?
            """, (
                prop.get("account_id", 1),
                prop["name"], prop.get("platform", "airbnb"),
                prop.get("listing_url", ""), prop.get("listing_id", ""),
                prop.get("city", ""), prop.get("latitude"), prop.get("longitude"),
                prop.get("min_price", 50), prop.get("max_price", 500),
                prop.get("sync_mode", "advisory"),
                prop.get("strategy", "balanced"),
                prop.get("plan", "free"),
                now, prop["id"],
            ))
            return prop["id"]
        else:
            if prop.get("id"):
                conn.execute("""
                    INSERT INTO properties
                        (id, account_id, name, platform, listing_url, listing_id, city,
                         latitude, longitude, min_price, max_price,
                         sync_mode, strategy, plan, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    prop["id"],
                    prop.get("account_id", 1),
                    prop["name"], prop.get("platform", "airbnb"),
                    prop.get("listing_url", ""), prop.get("listing_id", ""),
                    prop.get("city", ""), prop.get("latitude"), prop.get("longitude"),
                    prop.get("min_price", 50), prop.get("max_price", 500),
                    prop.get("sync_mode", "advisory"),
                    prop.get("strategy", "balanced"),
                    prop.get("plan", "free"),
                    now, now,
                ))
                return prop["id"]
            cur = conn.execute("""
                INSERT INTO properties
                    (account_id, name, platform, listing_url, listing_id, city,
                     latitude, longitude, min_price, max_price,
                     sync_mode, strategy, plan, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                prop.get("account_id", 1),
                prop["name"], prop.get("platform", "airbnb"),
                prop.get("listing_url", ""), prop.get("listing_id", ""),
                prop.get("city", ""), prop.get("latitude"), prop.get("longitude"),
                prop.get("min_price", 50), prop.get("max_price", 500),
                prop.get("sync_mode", "advisory"),
                prop.get("strategy", "balanced"),
                prop.get("plan", "free"),
                now, now,
            ))
            return cur.lastrowid


def get_properties() -> List[Dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM properties ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def get_property(prop_id: int) -> Optional[Dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM properties WHERE id=?", (prop_id,)).fetchone()
    return dict(row) if row else None


def delete_property(prop_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM properties WHERE id=?", (prop_id,))


# ─────────────────────────────────────────────
# DECISION LOG
# ─────────────────────────────────────────────

def save_decision_log(entry: Dict) -> int:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO decision_log
                (timestamp, account_id, property_id, old_price, new_price,
                 market_avg, occupancy, decision, mode, applied, notes,
                 date, competitor_avg, strategy, factors, mpi,
                 current_price_source, data_source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            entry.get("timestamp", now),
            entry.get("account_id", 1),
            entry.get("property_id", 1),
            entry["old_price"],
            entry["new_price"],
            entry.get("market_avg"),
            entry.get("occupancy"),
            entry.get("decision", ""),
            entry.get("mode", "advisory"),
            int(entry.get("applied", 0)),
            entry.get("notes", ""),
            entry.get("date"),            # data calendario (YYYY-MM-DD)
            entry.get("competitor_avg"),  # media competitor raw
            entry.get("strategy"),        # strategia pricing usata
            entry.get("factors"),         # breakdown fattori (JSON)
            entry.get("mpi"),             # Market Price Index
            entry.get("current_price_source", "manual"),
            entry.get("data_source", "demo"),
        ))
        return cur.lastrowid


def get_decision_log(
    limit: int = 200,
    property_id: Optional[int] = None,
    account_id: Optional[int] = None,
) -> List[Dict]:
    query = "SELECT * FROM decision_log WHERE 1=1"
    params: list = []
    if account_id is not None:
        query += " AND account_id=?"
        params.append(account_id)
    if property_id is not None:
        query += " AND property_id=?"
        params.append(property_id)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# OCCUPANCY HISTORY
# ─────────────────────────────────────────────

def save_occupancy(
    property_id: int,
    date_str: str,
    occupancy: float,
    source: str = "manual",
    account_id: int = 1,
) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO occupancy_history
                (account_id, property_id, date, occupancy, source)
            VALUES (?,?,?,?,?)
        """, (account_id, property_id, date_str, occupancy, source))


def get_occupancy_history(
    property_id: int = 1,
    limit: int = 90,
    account_id: Optional[int] = None,
) -> List[Dict]:
    query = "SELECT * FROM occupancy_history WHERE property_id=?"
    params: list = [property_id]
    if account_id is not None:
        query += " AND account_id=?"
        params.append(account_id)
    query += " ORDER BY date DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# MARKET HISTORY
# ─────────────────────────────────────────────

def save_market_history(entry: Dict) -> None:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO market_history
                (account_id, property_id, date, market_avg, market_min, market_max,
                 market_std, competitor_count, source, recorded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            entry.get("account_id", 1),
            entry.get("property_id", 1),
            entry["date"],
            entry.get("market_avg"),
            entry.get("market_min"),
            entry.get("market_max"),
            entry.get("market_std"),
            entry.get("competitor_count"),
            entry.get("source", "demo"),
            now,
        ))


def get_market_history(
    property_id: int = 1,
    limit: int = 90,
    account_id: Optional[int] = None,
) -> List[Dict]:
    query = "SELECT * FROM market_history WHERE property_id=?"
    params: list = [property_id]
    if account_id is not None:
        query += " AND account_id=?"
        params.append(account_id)
    query += " ORDER BY date DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# TELEGRAM LINKS
# ─────────────────────────────────────────────

# ---------------------------------------------------------------------------
# PRICE CALENDAR
# ---------------------------------------------------------------------------

def get_calendar_price(
    property_id: int,
    date_str: str,
    account_id: Optional[int] = None,
) -> Optional[Dict]:
    """Ritorna il prezzo calendario per una proprieta/data, se presente."""
    query = "SELECT * FROM price_calendar WHERE property_id=? AND date=?"
    params: list = [property_id, date_str]
    if account_id is not None:
        query += " AND account_id=?"
        params.append(account_id)
    query += " ORDER BY id DESC LIMIT 1"
    with get_conn() as conn:
        row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def get_price_calendar(
    account_id: Optional[int] = None,
    property_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 180,
) -> List[Dict]:
    """Lista prezzi correnti/raccomandati salvati nel calendario interno."""
    query = "SELECT * FROM price_calendar WHERE 1=1"
    params: list = []
    if account_id is not None:
        query += " AND account_id=?"
        params.append(account_id)
    if property_id is not None:
        query += " AND property_id=?"
        params.append(property_id)
    if date_from:
        query += " AND date>=?"
        params.append(date_from)
    if date_to:
        query += " AND date<=?"
        params.append(date_to)
    query += " ORDER BY date ASC, property_id ASC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def upsert_calendar_price(entry: Dict) -> Dict:
    """
    Inserisce o aggiorna un prezzo nel calendario.

    Oggi serve per i prezzi manuali e le raccomandazioni; domani lo stesso record
    verra aggiornato da PMS/channel manager reali.
    """
    now = datetime.utcnow().isoformat()
    account_id = int(entry.get("account_id") or 1)
    property_id = int(entry["property_id"])
    date_str = entry["date"]
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO price_calendar
                (account_id, property_id, date, current_price,
                 current_price_source, recommended_price, status,
                 decision_log_id, applied_price, notes, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id, property_id, date)
            DO UPDATE SET
                current_price=excluded.current_price,
                current_price_source=excluded.current_price_source,
                recommended_price=excluded.recommended_price,
                status=excluded.status,
                decision_log_id=excluded.decision_log_id,
                applied_price=excluded.applied_price,
                notes=excluded.notes,
                updated_at=excluded.updated_at
        """, (
            account_id,
            property_id,
            date_str,
            float(entry["current_price"]),
            entry.get("current_price_source", "manual"),
            entry.get("recommended_price"),
            entry.get("status", "current"),
            entry.get("decision_log_id"),
            entry.get("applied_price"),
            entry.get("notes", ""),
            entry.get("created_at", now),
            now,
        ))
    return get_calendar_price(property_id, date_str, account_id) or {}


def get_current_price_for_date(prop: Dict, date_str: str) -> tuple[float, str]:
    """
    Determina il prezzo corrente usato dal motore.

    Priorita: calendario interno -> eventuale campo current_price -> midpoint
    min/max. Il midpoint resta solo fallback, marcato esplicitamente.
    """
    account_id = int(prop.get("account_id") or 1)
    property_id = int(prop.get("id") or 1)
    calendar_row = get_calendar_price(property_id, date_str, account_id)
    if calendar_row and calendar_row.get("current_price") is not None:
        if str(calendar_row.get("status") or "").lower() == "locked":
            return (
                float(calendar_row["current_price"]),
                "manual_lock",
            )
        return (
            float(calendar_row["current_price"]),
            str(calendar_row.get("current_price_source") or "manual"),
        )

    if prop.get("current_price") is not None:
        return float(prop["current_price"]), "property_current_price"

    min_price = float(prop.get("min_price", 50))
    max_price = float(prop.get("max_price", 500))
    return (min_price + max_price) / 2, "price_range_midpoint"


def save_price_recommendation(
    account_id: int,
    property_id: int,
    date_str: str,
    current_price: float,
    recommended_price: float,
    status: str,
    decision_log_id: Optional[int] = None,
    notes: str = "",
    current_price_source: str = "manual",
) -> Dict:
    """Salva la raccomandazione nel calendario senza dichiararla applicata."""
    return upsert_calendar_price({
        "account_id": account_id,
        "property_id": property_id,
        "date": date_str,
        "current_price": current_price,
        "current_price_source": current_price_source,
        "recommended_price": recommended_price,
        "status": status,
        "decision_log_id": decision_log_id,
        "notes": notes,
    })


def update_calendar_status_for_decision(
    decision_log_id: int,
    status: str,
    applied_price: Optional[float] = None,
    notes: Optional[str] = None,
) -> Optional[Dict]:
    """Aggiorna lo stato calendario collegato a una decisione."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM price_calendar WHERE decision_log_id=? ORDER BY id DESC LIMIT 1",
            (decision_log_id,),
        ).fetchone()
        if not row:
            return None
        merged_notes = notes if notes is not None else row["notes"]
        conn.execute("""
            UPDATE price_calendar
               SET status=?, applied_price=?, notes=?, updated_at=?
             WHERE id=?
        """, (status, applied_price, merged_notes, now, row["id"]))
        updated = conn.execute(
            "SELECT * FROM price_calendar WHERE id=?",
            (row["id"],),
        ).fetchone()
    return dict(updated) if updated else None


def save_telegram_link(entry: Dict) -> int:
    """Inserisce o aggiorna un collegamento Telegram. Ritorna l'id."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM telegram_links WHERE id=?", (entry.get("id"),)
        ).fetchone()
        if existing:
            conn.execute("""
                UPDATE telegram_links SET
                    chat_id=?, telegram_username=?, active=?, updated_at=?
                WHERE id=?
            """, (
                entry.get("chat_id"),
                entry.get("telegram_username", ""),
                int(entry.get("active", 1)),
                now,
                entry["id"],
            ))
            return entry["id"]
        else:
            cur = conn.execute("""
                INSERT INTO telegram_links
                    (property_id, token, chat_id, telegram_username,
                     active, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?)
            """, (
                entry["property_id"],
                entry["token"],
                entry.get("chat_id"),
                entry.get("telegram_username", ""),
                int(entry.get("active", 1)),
                now, now,
            ))
            return cur.lastrowid


def get_telegram_link_by_token(token: str) -> Optional[Dict]:
    """Cerca un link per token (usato dal webhook /start)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM telegram_links WHERE token=?", (token,)
        ).fetchone()
    return dict(row) if row else None


def get_telegram_link_by_property(property_id: int) -> Optional[Dict]:
    """Restituisce il link attivo per una proprietà, se esiste."""
    with get_conn() as conn:
        row = conn.execute("""
            SELECT * FROM telegram_links
            WHERE property_id=? AND active=1
            ORDER BY id DESC LIMIT 1
        """, (property_id,)).fetchone()
    return dict(row) if row else None


def revoke_telegram_link(property_id: int) -> None:
    """Disattiva tutti i link attivi per una proprietà."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE telegram_links SET active=0 WHERE property_id=?",
            (property_id,)
        )


def get_all_telegram_links() -> List[Dict]:
    """Ritorna tutti i link (per la dashboard admin)."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT tl.*, p.name AS property_name
            FROM telegram_links tl
            LEFT JOIN properties p ON p.id = tl.property_id
            ORDER BY tl.property_id, tl.id DESC
        """).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# PROPERTY INTEGRATIONS (Multi-OTA)
# ─────────────────────────────────────────────

def record_telegram_approval(entry: Dict) -> int:
    """Registra un click Approva/Rifiuta arrivato da Telegram o dashboard."""
    now = datetime.utcnow().isoformat()
    payload = entry.get("payload") or {}
    if isinstance(payload, str):
        payload_json = payload
    else:
        payload_json = json.dumps(payload, ensure_ascii=False)

    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO telegram_approvals
                (timestamp, account_id, property_id, decision_log_id,
                 telegram_link_id, chat_id, telegram_username, action, status,
                 source, message_id, callback_query_id, error, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry.get("timestamp") or now,
            int(entry.get("account_id") or 1),
            entry.get("property_id"),
            int(entry.get("decision_log_id")),
            entry.get("telegram_link_id"),
            entry.get("chat_id"),
            entry.get("telegram_username", ""),
            entry.get("action", ""),
            entry.get("status", ""),
            entry.get("source", "telegram"),
            str(entry.get("message_id", "") or ""),
            str(entry.get("callback_query_id", "") or ""),
            str(entry.get("error", "") or ""),
            payload_json,
        ))
        return cur.lastrowid


def get_telegram_approvals(
    limit: int = 100,
    account_id: Optional[int] = None,
    property_id: Optional[int] = None,
    decision_log_id: Optional[int] = None,
) -> List[Dict]:
    """Ritorna lo storico approvazioni/rifiuti Telegram e dashboard."""
    query = """
        SELECT ta.*, p.name AS property_name
          FROM telegram_approvals ta
          LEFT JOIN properties p ON p.id = ta.property_id
         WHERE 1=1
    """
    params: list = []
    if account_id is not None:
        query += " AND ta.account_id=?"
        params.append(int(account_id))
    if property_id is not None:
        query += " AND ta.property_id=?"
        params.append(int(property_id))
    if decision_log_id is not None:
        query += " AND ta.decision_log_id=?"
        params.append(int(decision_log_id))
    query += " ORDER BY ta.timestamp DESC, ta.id DESC LIMIT ?"
    params.append(int(limit))
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_property_integrations(property_id: int) -> List[Dict]:
    """Ritorna tutte le integrazioni OTA per una proprietà."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM property_integrations WHERE property_id=? ORDER BY is_primary DESC, id ASC",
            (property_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_property_integration(entry: Dict) -> int:
    """
    Inserisce o aggiorna un'integrazione OTA per una proprietà.
    La chiave unica è (property_id, platform).
    Ritorna l'id della riga.
    """
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO property_integrations
                (property_id, platform, listing_url, listing_id, is_primary, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(property_id, platform)
            DO UPDATE SET
                listing_url = excluded.listing_url,
                listing_id  = excluded.listing_id,
                is_primary  = excluded.is_primary
        """, (
            entry["property_id"],
            entry["platform"],
            entry.get("listing_url", ""),
            entry.get("listing_id", ""),
            int(entry.get("is_primary", 0)),
            now,
        ))
    return cur.lastrowid


def delete_property_integration(integration_id: int) -> None:
    """Rimuove un'integrazione OTA per id."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM property_integrations WHERE id=?",
            (integration_id,)
        )


def update_decision_tg_message(log_id: int, tg_message_id: int) -> None:
    """Salva il message_id Telegram sulla decisione (per l'edit successivo)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE decision_log SET tg_message_id=? WHERE id=?",
            (tg_message_id, log_id)
        )


def get_pending_approvals(
    property_id: Optional[int] = None,
    account_id: Optional[int] = None,
) -> List[Dict]:
    """Ritorna le decisioni in attesa di approvazione."""
    query = (
        "SELECT * FROM decision_log "
        "WHERE mode='approval' AND applied=0 "
        "AND decision NOT LIKE '%[APPROVED%' "
        "AND decision NOT LIKE '%[REJECTED]%'"
    )
    params: list = []
    if account_id is not None:
        query += " AND account_id=?"
        params.append(account_id)
    if property_id is not None:
        query += " AND property_id=?"
        params.append(property_id)
    query += " ORDER BY timestamp DESC"
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Facade helpers shared by SQLite development and Supabase primary runtimes.
# ---------------------------------------------------------------------------

def get_decision_log_entry(log_id: int, account_id: Optional[int] = None) -> Optional[Dict]:
    query = "SELECT * FROM decision_log WHERE id=?"
    params: list[Any] = [int(log_id)]
    if account_id is not None:
        query += " AND account_id=?"
        params.append(int(account_id))
    with get_conn() as conn:
        row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def update_decision_state(
    log_id: int,
    *,
    account_id: int,
    applied: bool,
    decision: str,
) -> Optional[Dict]:
    with get_conn() as conn:
        conn.execute(
            "UPDATE decision_log SET applied=?, decision=? WHERE id=? AND account_id=?",
            (int(bool(applied)), str(decision), int(log_id), int(account_id)),
        )
    return get_decision_log_entry(log_id, account_id)


def mark_decision_rejected(log_id: int, account_id: int) -> Optional[Dict]:
    row = get_decision_log_entry(log_id, account_id)
    if not row:
        return None
    decision = str(row.get("decision") or "")
    if "[REJECTED]" not in decision:
        decision = f"{decision} [REJECTED]".strip()
    return update_decision_state(
        log_id,
        account_id=int(account_id),
        applied=False,
        decision=decision,
    )


def get_telegram_decision_context(log_id: int, chat_id: int) -> Optional[Dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT account_id, property_id FROM decision_log WHERE id=?",
            (int(log_id),),
        ).fetchone()
        if not row:
            return None
        link = conn.execute(
            """SELECT id, telegram_username FROM telegram_links
               WHERE property_id=? AND chat_id=? AND active=1
               ORDER BY id DESC LIMIT 1""",
            (int(row["property_id"]), int(chat_id)),
        ).fetchone()
    if not link:
        return None
    return {
        "id": int(log_id),
        "account_id": int(row["account_id"] or 1),
        "property_id": int(row["property_id"]),
        "telegram_link_id": int(link["id"]),
        "telegram_username": str(link["telegram_username"] or ""),
    }


def record_price_update(prop: Dict, result: Dict, target_date: date) -> int:
    """Storico applicazioni OTA, usato anche quando il channel manager e stub."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS price_updates (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   account_id INTEGER NOT NULL DEFAULT 1,
                   property_id INTEGER NOT NULL,
                   platform TEXT, listing_id TEXT, target_date TEXT,
                   new_price REAL, ok INTEGER NOT NULL DEFAULT 0,
                   error TEXT, applied_at TEXT, is_stub INTEGER NOT NULL DEFAULT 0
               )"""
        )
        cursor = conn.execute(
            """INSERT INTO price_updates
               (account_id, property_id, platform, listing_id, target_date, new_price,
                ok, error, applied_at, is_stub)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                int(prop.get("account_id") or 1),
                int(prop["id"]),
                str(result.get("platform") or prop.get("platform") or ""),
                str(result.get("listing_id") or prop.get("listing_id") or ""),
                target_date.isoformat(),
                result.get("new_price"),
                int(bool(result.get("ok"))),
                str(result.get("error") or ""),
                result.get("applied_at") or now,
                int(bool((result.get("raw") or {}).get("stub", False))),
            ),
        )
    return int(cursor.lastrowid)


def get_price_updates(property_ids: List[int], limit: int = 500) -> List[Dict]:
    if not property_ids:
        return []
    placeholders = ",".join("?" for _ in property_ids)
    with get_conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS price_updates (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   account_id INTEGER NOT NULL DEFAULT 1,
                   property_id INTEGER NOT NULL,
                   platform TEXT, listing_id TEXT, target_date TEXT,
                   new_price REAL, ok INTEGER NOT NULL DEFAULT 0,
                   error TEXT, applied_at TEXT, is_stub INTEGER NOT NULL DEFAULT 0
               )"""
        )
        rows = conn.execute(
            f"SELECT * FROM price_updates WHERE property_id IN ({placeholders}) "
            "ORDER BY applied_at DESC LIMIT ?",
            [int(item) for item in property_ids] + [int(limit)],
        ).fetchall()
    return [dict(row) for row in rows]


def _cloud_or_sqlite(name: str, local_function):
    """Usa Supabase come fonte unica senza un fallback locale silenzioso."""
    @wraps(local_function)
    def wrapped(*args, **kwargs):
        if is_supabase_primary():
            # Streamlit e codice server-side: anche il ripristino sessione deve
            # funzionare senza dipendere da una sessione browser Supabase.
            require_supabase_primary(require_service_role=True)
            from pricepilot.services.supabase_primary import dispatch
            return dispatch(name, *args, **kwargs)
        return local_function(*args, **kwargs)
    return wrapped


_CLOUD_PRIMARY_OPERATIONS = (
    "create_account", "get_account", "update_account",
    "get_user", "get_user_by_email", "get_users", "create_user", "update_user", "delete_user",
    "create_auth_session", "get_user_by_auth_session", "revoke_auth_session",
    "record_user_consent", "get_latest_user_consent",
    "ensure_default_guardrail_policy", "get_guardrail_policy", "update_guardrail_policy",
    "count_auto_actions_today", "record_audit_event", "get_audit_events",
    "start_operation_run", "try_start_operation_run", "get_active_operation_run", "finish_operation_run",
    "get_operation_run", "get_operation_runs", "get_last_operation_run",
    "ensure_default_notification_preferences", "get_notification_preferences",
    "update_notification_preferences", "record_notification_log", "get_notification_log",
    "upsert_property", "get_properties", "get_property", "delete_property",
    "save_decision_log", "get_decision_log", "get_decision_log_entry", "update_decision_state",
    "mark_decision_rejected", "save_occupancy", "get_occupancy_history", "save_market_history",
    "get_market_history", "get_calendar_price", "get_price_calendar", "upsert_calendar_price",
    "get_current_price_for_date", "save_price_recommendation", "update_calendar_status_for_decision",
    "save_telegram_link", "get_telegram_link_by_token", "get_telegram_link_by_property",
    "revoke_telegram_link", "get_all_telegram_links", "get_telegram_decision_context",
    "record_telegram_approval", "get_telegram_approvals", "get_pending_approvals",
    "get_property_integrations", "upsert_property_integration", "delete_property_integration",
    "update_decision_tg_message", "record_price_update", "get_price_updates",
    "save_decision", "get_decisions", "save_competitors", "get_competitors", "upsert_event",
    "get_events", "save_market_snapshot", "get_market_snapshots", "get_summary_stats",
)

for _operation_name in _CLOUD_PRIMARY_OPERATIONS:
    _operation = globals().get(_operation_name)
    if _operation is not None:
        globals()[_operation_name] = _cloud_or_sqlite(_operation_name, _operation)
