"""
PricePilot - Configuration Manager
Gestione centralizzata della configurazione con persistenza su file JSON.
Legge anche le variabili d'ambiente da .env nella root del progetto.
"""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_FILE = BASE_DIR / "data" / "config.json"
ENV_FILE    = BASE_DIR / ".env"


def _load_dotenv() -> None:
    """Carica le variabili da .env senza dipendenze esterne."""
    if not ENV_FILE.exists():
        return
    with ENV_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key   = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value


# Carica .env all'import del modulo
_load_dotenv()

DEFAULT_CONFIG = {
    # Proprietà
    "property_name": "My Airbnb Property",
    "property_id": "default",
    "location": "Italy",

    # Prezzi
    "base_price": 80.0,
    "min_price_per_night": 50.0,
    "max_price_per_night": 500.0,
    "max_change_pct": 0.20,      # Max ±20% per aggiornamento

    # Strategia
    # conservative | balanced | aggressive | premium
    "strategy": "balanced",

    # Occupancy thresholds
    "occupancy_low_threshold": 0.30,    # sotto: abbassa prezzo
    "occupancy_high_threshold": 0.80,   # sopra: alza prezzo
    "occupancy_low_multiplier": 0.90,
    "occupancy_high_multiplier": 1.15,

    # Competizione
    "competitor_weight": 0.50,   # peso prezzo competitor vs base

    # Aggiornamento automatico
    "update_interval_hours": 6,

    # Notifiche
    "telegram_token": "",
    "telegram_chat_id": "",

    # Database
    "db_path": str(BASE_DIR / "data" / "pricepilot.db"),
}


def load_config() -> dict:
    """Carica configurazione da file JSON, con fallback ai default."""
    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open("r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            cfg = {**DEFAULT_CONFIG, **user_cfg}
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    """Salva configurazione su file JSON."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_FILE.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# Istanza globale
CONFIG = load_config()
