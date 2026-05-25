"""
PricePilot - Location Context Analyzer

Classifica il contesto geografico di una proprietà e restituisce
fattori di aggiustamento per weekend, eventi e volatilità della domanda.

In produzione sostituire detect_location_context() con chiamate a:
  - Overpass API (OpenStreetMap): rileva strutture vicine (mare, stazioni sci, etc.)
  - Google Places API: classifica il tipo di zona
  - Nominatim: geocoding inverso

8 tipi di contesto supportati:
  city_center, beach_resort, mountain, airport_area,
  business_district, university_area, rural, tourist_village
"""

from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class LocationContext:
    context_type: str           # chiave identificativa
    label: str                  # etichetta leggibile
    icon: str                   # emoji icona
    weekend_sensitivity: float  # moltiplicatore domanda weekend (1.0 = normale)
    event_sensitivity: float    # moltiplicatore per eventi speciali (1.0 = normale)
    demand_volatility: float    # volatilità domanda 0.0–1.0 (0 = stabile, 1 = molto variabile)
    description: str = ""       # descrizione per UI


# ─── Profili di contesto ──────────────────────────────────────────────────────

_CONTEXT_PROFILES: dict = {
    "city_center": LocationContext(
        context_type    = "city_center",
        label           = "Centro città",
        icon            = "🏙️",
        weekend_sensitivity = 1.25,
        event_sensitivity   = 1.30,
        demand_volatility   = 0.45,
        description = "Alta domanda tutto l'anno con picchi weekend e eventi.",
    ),
    "beach_resort": LocationContext(
        context_type    = "beach_resort",
        label           = "Zona balneare",
        icon            = "🏖️",
        weekend_sensitivity = 1.40,
        event_sensitivity   = 1.10,
        demand_volatility   = 0.65,
        description = "Fortissima stagionalità estiva, weekend molto premiati.",
    ),
    "mountain": LocationContext(
        context_type    = "mountain",
        label           = "Montagna / Sci",
        icon            = "⛰️",
        weekend_sensitivity = 1.50,
        event_sensitivity   = 1.05,
        demand_volatility   = 0.55,
        description = "Picchi weekend e vacanze invernali/estive, bassa media.",
    ),
    "airport_area": LocationContext(
        context_type    = "airport_area",
        label           = "Area aeroportuale",
        icon            = "✈️",
        weekend_sensitivity = 1.05,
        event_sensitivity   = 1.15,
        demand_volatility   = 0.30,
        description = "Domanda costante da viaggiatori business e transit.",
    ),
    "business_district": LocationContext(
        context_type    = "business_district",
        label           = "Distretto business",
        icon            = "💼",
        weekend_sensitivity = 0.80,
        event_sensitivity   = 1.20,
        demand_volatility   = 0.35,
        description = "Alta domanda infrasettimanale, bassa nel weekend.",
    ),
    "university_area": LocationContext(
        context_type    = "university_area",
        label           = "Zona universitaria",
        icon            = "🎓",
        weekend_sensitivity = 1.15,
        event_sensitivity   = 1.40,
        demand_volatility   = 0.40,
        description = "Picchi per lauree, open day e eventi culturali.",
    ),
    "rural": LocationContext(
        context_type    = "rural",
        label           = "Campagna / Rurale",
        icon            = "🌾",
        weekend_sensitivity = 1.35,
        event_sensitivity   = 0.90,
        demand_volatility   = 0.50,
        description = "Domanda concentrata nei weekend e festività.",
    ),
    "tourist_village": LocationContext(
        context_type    = "tourist_village",
        label           = "Borgo turistico",
        icon            = "🏘️",
        weekend_sensitivity = 1.30,
        event_sensitivity   = 1.15,
        demand_volatility   = 0.55,
        description = "Stagionalità mista: turismo culturale e weekend.",
    ),
}

# Default se lat/lon non disponibili
_DEFAULT_CONTEXT = "city_center"


# ─── Euristica geografica semplice (Italia) ───────────────────────────────────

def _detect_from_coordinates(lat: float, lon: float) -> str:
    """
    Classifica il contesto geografico tramite euristica su lat/lon.
    Copertura: Italia. Estendere con API esterne per precisione maggiore.
    """
    # Alpi / Dolomiti (Nord Italia, alta quota)
    if 45.5 <= lat <= 47.5 and 6.5 <= lon <= 14.5:
        return "mountain"
    # Appennino / Montagna Centro-Sud
    if 42.0 <= lat <= 45.5 and 12.5 <= lon <= 16.0:
        return "mountain"
    # Costa Adriatica (Rimini, Riccione, Pesaro)
    if 43.0 <= lat <= 44.5 and 12.0 <= lon <= 14.5:
        return "beach_resort"
    # Costa Tirrenica Nord (Versilia, Cinque Terre)
    if 43.5 <= lat <= 44.5 and 9.5 <= lon <= 10.5:
        return "beach_resort"
    # Costa Sud (Puglia, Calabria, Sicilia)
    if lat < 40.5:
        return "beach_resort"
    # Grandi città (Roma, Milano, Napoli, Torino, Bologna, Firenze)
    _CITIES = [
        (41.89, 12.48, "city_center"),   # Roma
        (45.46, 9.19,  "city_center"),   # Milano
        (40.85, 14.27, "city_center"),   # Napoli
        (45.07, 7.69,  "city_center"),   # Torino
        (44.50, 11.34, "city_center"),   # Bologna
        (43.77, 11.25, "city_center"),   # Firenze
        (45.44, 12.33, "tourist_village"),  # Venezia
        (37.50, 15.09, "tourist_village"),  # Catania
    ]
    for clat, clon, ctype in _CITIES:
        if abs(lat - clat) < 0.15 and abs(lon - clon) < 0.20:
            return ctype
    # Fallback
    return "city_center"


# ─── API pubblica ─────────────────────────────────────────────────────────────

def detect_location_context(
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    context_type: Optional[str] = None,
) -> LocationContext:
    """
    Classifica il contesto geografico della proprietà.

    Args:
        lat:          Latitudine (opzionale – per euristica automatica)
        lon:          Longitudine (opzionale – per euristica automatica)
        context_type: Override manuale del tipo (es. "beach_resort").
                      Usato se già noto (es. da form proprietà).

    Returns:
        LocationContext con fattori di aggiustamento pricing.
    """
    if context_type and context_type in _CONTEXT_PROFILES:
        return _CONTEXT_PROFILES[context_type]
    if lat is not None and lon is not None:
        detected = _detect_from_coordinates(lat, lon)
        return _CONTEXT_PROFILES[detected]
    return _CONTEXT_PROFILES[_DEFAULT_CONTEXT]


def get_context_adjustment(
    context: LocationContext,
    is_weekend: bool,
    has_event: bool,
) -> float:
    """
    Calcola un moltiplicatore di aggiustamento pricing basato sul contesto.

    Usato in simulate_competitors() per rendere i prezzi simulati più
    realistici in base alla tipologia di zona.

    Returns:
        Moltiplicatore (es. 1.12 = +12% rispetto alla media nazionale)
    """
    factor = 1.0
    if is_weekend:
        # L'effetto weekend è pesato al 50% del sensitivity del contesto
        factor *= 1.0 + (context.weekend_sensitivity - 1.0) * 0.50
    if has_event:
        # L'effetto evento è pesato al 30% del sensitivity del contesto
        factor *= 1.0 + (context.event_sensitivity - 1.0) * 0.30
    return round(factor, 3)


def list_context_types() -> List[LocationContext]:
    """Ritorna tutti i profili di contesto disponibili."""
    return list(_CONTEXT_PROFILES.values())


def get_context_by_type(context_type: str) -> LocationContext:
    """Ritorna il profilo per nome, fallback city_center."""
    return _CONTEXT_PROFILES.get(context_type, _CONTEXT_PROFILES[_DEFAULT_CONTEXT])
