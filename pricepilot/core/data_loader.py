"""
PricePilot - Data Loader
Caricamento dati da CSV (compatibilità backward) e conversione in formato interno.
"""
import csv
from pathlib import Path
from typing import List, Dict

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_PATH = BASE_DIR / "data" / "sample_data.csv"


def load_sample_data(path: str = None) -> List[Dict]:
    """
    Carica dati da CSV.
    Formato atteso: date, price, occupancy, competitor_price, event
    """
    p = Path(path) if path else DATA_PATH
    rows = []
    if not p.exists():
        return rows
    with p.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                rows.append({
                    "date":             row["date"].strip(),
                    "price":            float(row["price"]),
                    "occupancy":        float(row["occupancy"]),
                    "competitor_price": float(row["competitor_price"]),
                    "event":            row.get("event", "").strip(),
                })
            except (ValueError, KeyError):
                continue
    return rows
