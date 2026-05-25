"""
PricePilot - Events Data Source
Gestione eventi locali che impattano i prezzi.

In produzione integrare con:
  - Eventbrite API
  - Google Events
  - Ticketmaster API
  - Feed locali (comuni, turismo)
"""
from datetime import date, timedelta
from typing import List, Dict, Optional


# ─── Catalogo eventi demo ─────────────────────────────────────────────────────
DEMO_EVENTS = [
    # formato: (month, day, name, type, impact_level, description)
    (1,  1,  "Capodanno",           "holiday",    "high",   "Festività nazionale"),
    (2,  14, "San Valentino",       "holiday",    "medium", "Weekend romantico"),
    (4,  25, "Festa della Lib.",    "holiday",    "high",   "Festività nazionale"),
    (5,  1,  "Festa del Lavoro",    "holiday",    "high",   "Festività nazionale"),
    (6,  2,  "Festa Repubblica",    "holiday",    "high",   "Festività nazionale"),
    (7,  15, "Festival Estivo",     "festival",   "high",   "Grande festival locale"),
    (7,  20, "Concerto Arena",      "concert",    "high",   "Concerto internazionale"),
    (8,  10, "Ferragosto",          "holiday",    "high",   "Festività nazionale"),
    (8,  15, "Fiera Gastr.",        "fair",       "medium", "Fiera gastronomica"),
    (9,  5,  "Conferenza Tech",     "conference", "high",   "Conferenza internazionale"),
    (9,  20, "Maratona Città",      "marathon",   "medium", "Gara podistica"),
    (10, 31, "Halloween",           "holiday",    "medium", "Evento commerciale"),
    (11, 1,  "Ognissanti",          "holiday",    "medium", "Festività nazionale"),
    (12, 8,  "Immacolata",          "holiday",    "high",   "Festività nazionale"),
    (12, 24, "Vigilia Natale",      "holiday",    "high",   "Festività nazionale"),
    (12, 25, "Natale",              "holiday",    "high",   "Festività nazionale"),
    (12, 26, "Santo Stefano",       "holiday",    "high",   "Festività nazionale"),
    (12, 31, "San Silvestro",       "holiday",    "high",   "Festività nazionale"),
]


def get_events_for_period(
    date_from: date,
    date_to: date,
    year: Optional[int] = None,
) -> List[Dict]:
    """
    Ritorna gli eventi in un periodo.
    Usa l'anno di date_from se year non specificato.
    """
    y = year or date_from.year
    events = []
    for m, d, name, etype, impact, desc in DEMO_EVENTS:
        try:
            evt_date = date(y, m, d)
        except ValueError:
            continue
        if date_from <= evt_date <= date_to:
            events.append({
                "date":        evt_date.isoformat(),
                "name":        name,
                "event_type":  etype,
                "impact_level": impact,
                "description": desc,
            })
    return sorted(events, key=lambda x: x["date"])


def get_event_for_date(target_date: date) -> Optional[Dict]:
    """Ritorna l'evento più significativo per una data (None se nessuno)."""
    events = get_events_for_period(target_date, target_date, target_date.year)
    if not events:
        return None
    # Se multipli, prende il più impattante
    priority = {"high": 3, "medium": 2, "low": 1}
    return max(events, key=lambda e: priority.get(e["impact_level"], 0))


def event_to_string(event: Optional[Dict]) -> str:
    """Converte dict evento in stringa per il pricing engine."""
    if not event:
        return "none"
    return event.get("event_type", event.get("name", "generic")).lower()


def get_upcoming_events(days: int = 30) -> List[Dict]:
    """Ritorna eventi nei prossimi N giorni."""
    today = date.today()
    return get_events_for_period(today, today + timedelta(days=days))
