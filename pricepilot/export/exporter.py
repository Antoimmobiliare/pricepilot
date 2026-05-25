"""
PricePilot - Data Exporter
Export storico decisioni in CSV, JSON e Excel.
"""
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

from pricepilot.core.database import get_decisions


BASE_DIR = Path(__file__).resolve().parents[2]
EXPORT_DIR = BASE_DIR / "exports"


def ensure_export_dir() -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    return EXPORT_DIR


def export_csv(
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    output_path: Optional[str] = None,
    account_id: Optional[int] = None,
) -> str:
    """Esporta decisioni in CSV. Ritorna il path del file creato."""
    rows = get_decisions(
        limit=10000,
        date_from=date_from,
        date_to=date_to,
        account_id=account_id,
    )
    if not rows:
        return ""

    ts   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = Path(output_path) if output_path else ensure_export_dir() / f"decisions_{ts}.csv"

    fieldnames = [
        "id", "timestamp", "date", "property_id",
        "old_price", "new_price", "pct_change",
        "competitor_price", "market_price",
        "competitor_count", "competitor_min", "competitor_max",
        "occupancy", "event", "strategy", "decision", "applied",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


def export_json(
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    output_path: Optional[str] = None,
    account_id: Optional[int] = None,
) -> str:
    """Esporta decisioni in JSON. Ritorna il path del file creato."""
    rows = get_decisions(
        limit=10000,
        date_from=date_from,
        date_to=date_to,
        account_id=account_id,
    )
    if not rows:
        return ""

    ts   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = Path(output_path) if output_path else ensure_export_dir() / f"decisions_{ts}.json"

    with path.open("w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False, default=str)
    return str(path)


def export_summary_report(
    output_path: Optional[str] = None,
    account_id: Optional[int] = None,
) -> str:
    """Genera un report di riepilogo testuale."""
    rows = get_decisions(limit=10000, account_id=account_id)
    if not rows:
        return ""

    ts   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = Path(output_path) if output_path else ensure_export_dir() / f"report_{ts}.txt"

    prices     = [r["new_price"] for r in rows]
    avg_price  = sum(prices) / len(prices) if prices else 0
    max_price  = max(prices) if prices else 0
    min_price  = min(prices) if prices else 0
    changes    = [r["pct_change"] for r in rows]
    avg_change = sum(abs(c) for c in changes) / len(changes) * 100 if changes else 0

    with path.open("w", encoding="utf-8") as f:
        f.write("=" * 50 + "\n")
        f.write("     PRICEPILOT - REPORT RIEPILOGATIVO\n")
        f.write(f"     Generato: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n")
        f.write("=" * 50 + "\n\n")
        f.write(f"Totale decisioni:  {len(rows)}\n")
        f.write(f"Prezzo medio:      €{avg_price:.2f}\n")
        f.write(f"Prezzo massimo:    €{max_price:.2f}\n")
        f.write(f"Prezzo minimo:     €{min_price:.2f}\n")
        f.write(f"Variazione media:  {avg_change:.1f}%\n")
        f.write("\nUltime 10 decisioni:\n")
        f.write("-" * 50 + "\n")
        for r in rows[:10]:
            pct = r["pct_change"] * 100
            f.write(
                f"  {r['date']}  €{r['old_price']:.2f} → €{r['new_price']:.2f}"
                f"  ({'+' if pct >= 0 else ''}{pct:.1f}%)"
                f"  [{r.get('strategy', '-')}]\n"
            )
    return str(path)
