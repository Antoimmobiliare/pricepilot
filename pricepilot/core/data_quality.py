"""Explicit demo opt-in and fail-closed rules shared by all entry points."""
import math
import os


class DataUnavailable(ValueError):
    """A recommendation cannot safely be built from the available inputs."""


def live_mode() -> bool:
    return os.getenv("PRICEPILOT_ENV", "development").strip().lower() in {
        "production", "prod", "staging", "live",
    }


def demo_enabled() -> bool:
    return not live_mode() and os.getenv("PRICEPILOT_DATA_PROVIDER", "").lower() == "demo"


def calendar_pricing_enabled() -> bool:
    """Real operation uses own calendar only; the old market path is demo-only."""
    return not demo_enabled() or os.getenv("PRICEPILOT_PRICING_BASIS") == "calendar_only"


def validate_occupancy(value):
    if value is None or not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
        raise DataUnavailable("Occupazione non disponibile o non valida: collegare un calendario attendibile.")


def validate_market(result):
    source = str(result.source or "").lower()
    prices = [float(c.get("price", 0)) for c in result.competitors]
    if not prices or any(not math.isfinite(p) or p <= 0 for p in prices):
        raise DataUnavailable("Prezzi competitor non disponibili: nessun dato sostitutivo generato.")
    avg = float(result.market_stats.get("market_avg") or 0)
    if not math.isfinite(avg) or avg <= 0:
        raise DataUnavailable("Prezzo medio dei competitor non valido.")
    if not demo_enabled():
        if "demo" in source or "simulat" in source:
            raise DataUnavailable("Dati simulati esclusi dal percorso operativo.")
        if not result.raw.get("validated_observations"):
            raise DataUnavailable("La fonte mercato non fornisce osservazioni verificabili e datate.")
