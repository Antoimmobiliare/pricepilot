"""
PricePilot - CLI entry point.

Usage:
    python -m pricepilot.main --once
    python -m pricepilot.main --loop
    python -m pricepilot.main --date 2026-05-15
    python -m pricepilot.main --seed --days 30
"""
import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-25s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pricepilot.main")

from pricepilot.core.config import CONFIG
from pricepilot.core.database import init_db
from pricepilot.core.scheduler import (
    run_periodic,
    run_pricing_cycle as run_account_pricing_cycle,
)


def run_pricing_cycle(
    target_date: date = None,
    verbose: bool = True,
    source: str = "cli_manual",
) -> dict:
    """
    Run the unified SaaS pricing cycle for the local account.

    The old CLI used a separate legacy pricing path. Keeping this wrapper means
    CLI, dashboard and API now exercise the same scheduler/decision engine.
    """
    d = target_date or date.today()
    result = run_account_pricing_cycle(account_id=1, target_date=d, source=source)
    if verbose:
        run = result.get("run") or {}
        logger.info(
            "Ciclo %s completato: %s decisioni",
            d.isoformat(),
            run.get("decisions_count", 0),
        )
    return result


def seed_demo_data(days: int = 90) -> None:
    """Populate demo history using the same engine used in the app."""
    logger.info("Seeding %s giorni di dati demo...", days)
    today = date.today()
    for i in range(days, -1, -1):
        d = today - timedelta(days=i)
        run_pricing_cycle(d, verbose=(i % 10 == 0))
    logger.info("Seed completato: %s giorni inseriti.", days + 1)


def main():
    parser = argparse.ArgumentParser(
        description="PricePilot - Dynamic Pricing Engine",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--once", action="store_true", help="Esegui un ciclo e fermati")
    parser.add_argument("--loop", action="store_true", help="Loop continuo ogni N ore")
    parser.add_argument("--seed", action="store_true", help="Popola dati demo")
    parser.add_argument("--date", type=str, default=None, help="Data specifica YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=90, help="Giorni per --seed")
    args = parser.parse_args()

    init_db()
    logger.info("PricePilot avviato")

    if args.seed:
        seed_demo_data(args.days)
        return

    if args.date:
        try:
            d = date.fromisoformat(args.date)
        except ValueError:
            logger.error("Data non valida: %s. Usa formato YYYY-MM-DD", args.date)
            sys.exit(1)
        run_pricing_cycle(d)
        return

    if args.loop:
        hours = float(CONFIG.get("update_interval_hours", 6))
        run_periodic(lambda: run_pricing_cycle(source="cli_loop"), hours=hours, once=False)
        return

    run_pricing_cycle()


if __name__ == "__main__":
    main()
