"""
PricePilot - Seed Demo Data (v2)
Popola il database con dati demo completi:
  - properties (3 proprietà di esempio)
  - competitors (per ogni giorno)
  - market_history
  - occupancy_history
  - pricing_decisions (vecchio motore, compatibilità)
  - decision_log (nuovo motore)

Eseguire con:
    python seed_demo.py
    python seed_demo.py --days 90
    python seed_demo.py --reset   # cancella tutto e ricomincia
"""
import sys
import argparse
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pricepilot.core.database import (
    init_db, save_occupancy, save_market_history,
    save_decision_log, upsert_property, get_conn,
)
from pricepilot.main import seed_demo_data as _legacy_seed
from pricepilot.engine.market_analyzer import simulate_competitors, calculate_market_stats
from pricepilot.engine.pricing_engine import calculate_recommended_price
from datetime import date, timedelta
import hashlib


# ─── Proprietà demo ──────────────────────────────────────────────────────────
DEMO_PROPERTIES = [
    {
        "name":        "Studio Centro Storico",
        "platform":    "airbnb",
        "listing_url": "https://airbnb.com/rooms/demo_001",
        "listing_id":  "demo_001",
        "city":        "Rome",
        "latitude":    41.9028,
        "longitude":   12.4964,
        "min_price":   55.0,
        "max_price":   200.0,
        "sync_mode":   "advisory",
    },
    {
        "name":        "Appartamento Vista Mare",
        "platform":    "booking",
        "listing_url": "https://booking.com/hotel/demo_002",
        "listing_id":  "demo_002",
        "city":        "Amalfi",
        "latitude":    40.6340,
        "longitude":   14.6027,
        "min_price":   80.0,
        "max_price":   350.0,
        "sync_mode":   "approval",
    },
    {
        "name":        "Loft Moderno Business",
        "platform":    "airbnb",
        "listing_url": "https://airbnb.com/rooms/demo_003",
        "listing_id":  "demo_003",
        "city":        "Milan",
        "latitude":    45.4642,
        "longitude":   9.1900,
        "min_price":   90.0,
        "max_price":   400.0,
        "sync_mode":   "auto",
    },
]

DEMO_EVENTS = {
    "2025-12-24": "holiday",
    "2025-12-25": "holiday",
    "2025-12-31": "holiday",
    "2026-01-01": "holiday",
    "2026-02-14": "holiday",
    "2026-04-25": "holiday",
    "2026-05-01": "holiday",
}


def _det_noise(seed: str, spread: float = 0.3) -> float:
    h = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    return ((h % 1000) / 1000.0 - 0.5) * 2 * spread


def seed_properties() -> list:
    """Inserisce le proprietà demo. Ritorna lista id creati."""
    ids = []
    for p in DEMO_PROPERTIES:
        pid = upsert_property(p)
        ids.append(pid)
        print(f"  ✅ Proprietà: [{pid}] {p['name']} ({p['platform']}, {p['sync_mode']})")
    return ids


def seed_market_and_occupancy(property_ids: list, days: int) -> None:
    """Genera market_history e occupancy_history per ogni proprietà."""
    today = date.today()
    total = days * len(property_ids)
    done  = 0

    for idx, prop_id in enumerate(property_ids):
        prop_def   = DEMO_PROPERTIES[idx % len(DEMO_PROPERTIES)]
        base_price = prop_def["min_price"] + (prop_def["max_price"] - prop_def["min_price"]) * 0.5

        for i in range(days, -1, -1):
            d     = today - timedelta(days=i)
            event = DEMO_EVENTS.get(d.isoformat(), "none")

            # Competitor + market stats
            comps = simulate_competitors(base_price, d, event, n=10)
            stats = calculate_market_stats(comps)

            save_market_history({
                "property_id":      prop_id,
                "date":             d.isoformat(),
                "market_avg":       stats["market_avg"],
                "market_min":       stats["market_min"],
                "market_max":       stats["market_max"],
                "market_std":       stats["market_std"],
                "competitor_count": stats["competitor_count"],
            })

            # Occupancy simulata
            occ_seed = f"occ_{prop_id}_{d.isoformat()}"
            occ = round(max(0.10, min(0.98, 0.62 + _det_noise(occ_seed, 0.30))), 2)
            save_occupancy(prop_id, d.isoformat(), occ, source="simulated")

            # Decision log
            result = calculate_recommended_price(
                base_price       = base_price,
                market_avg       = stats["market_avg"],
                occupancy        = occ,
                target_date      = d,
                has_event        = event != "none",
                min_price        = prop_def["min_price"],
                max_price        = prop_def["max_price"],
                competitor_count = 10,
            )
            mode = prop_def["sync_mode"]
            save_decision_log({
                "property_id": prop_id,
                "old_price":   base_price,
                "new_price":   result["recommended_price"],
                "market_avg":  stats["market_avg"],
                "occupancy":   occ,
                "decision":    f"SEED_{mode.upper()}",
                "mode":        mode,
                "applied":     1 if mode == "auto" else 0,
            })

            done += 1
            if done % 30 == 0:
                pct = done / total * 100
                print(f"  Progress: {done}/{total} ({pct:.0f}%)")


def reset_db() -> None:
    """Cancella tutti i dati dalle tabelle principali."""
    with get_conn() as conn:
        for table in [
            "pricing_decisions", "competitors", "events",
            "market_snapshots", "properties", "decision_log",
            "occupancy_history", "market_history",
        ]:
            conn.execute(f"DELETE FROM {table}")
    print("  🗑️  Database resettato.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PricePilot – Seed Demo Data v2")
    parser.add_argument("--days",  type=int, default=90, help="Giorni di storico (default: 90)")
    parser.add_argument("--reset", action="store_true",  help="Cancella tutti i dati prima del seed")
    args = parser.parse_args()

    print("✈️  PricePilot – Seed Demo Data v2")
    print(f"   Giorni: {args.days}")
    print()

    init_db()

    if args.reset:
        print("🗑️  Reset database...")
        reset_db()

    print("🏠 Creazione proprietà demo...")
    prop_ids = seed_properties()
    print()

    print(f"📊 Generazione {args.days} giorni storico (market + occupancy + decisions)...")
    seed_market_and_occupancy(prop_ids, args.days)
    print()

    print("📅 Generazione legacy pricing_decisions (compatibilità dashboard v1)...")
    _legacy_seed(args.days)
    print()

    print("=" * 55)
    print("✅ SEED COMPLETATO!")
    print(f"   • {len(prop_ids)} proprietà")
    print(f"   • {(args.days + 1) * len(prop_ids)} record market_history")
    print(f"   • {(args.days + 1) * len(prop_ids)} record occupancy_history")
    print(f"   • {(args.days + 1) * len(prop_ids)} record decision_log")
    print()
    print("🚀 Avvia la dashboard con:")
    print("   streamlit run pricepilot/dashboard/app.py")
    print("=" * 55)
