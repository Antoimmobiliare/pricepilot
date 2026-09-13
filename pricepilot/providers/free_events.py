"""Free, automatic, location-aware event signals.

The provider works without manual data entry:
- Italian national holidays are available offline;
- Ticketmaster Discovery API adds nearby concerts, fairs, sport and festivals
  when a free ``TICKETMASTER_API_KEY`` is configured.

The result follows the common EventProvider contract, so a future official
city-feed provider can replace it without changing scheduling or pricing.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_GEOHASH_ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz"
_IMPACT_PRIORITY = {"high": 3, "medium": 2, "low": 1}


def _easter_sunday(year: int) -> date:
    """Gregorian computus; keeps national holiday signals offline and free."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _italian_holidays(year: int) -> list[dict]:
    fixed = [
        (1, 1, "Capodanno"),
        (1, 6, "Epifania"),
        (4, 25, "Festa della Liberazione"),
        (5, 1, "Festa del Lavoro"),
        (6, 2, "Festa della Repubblica"),
        (8, 15, "Ferragosto"),
        (11, 1, "Ognissanti"),
        (12, 8, "Immacolata Concezione"),
        (12, 25, "Natale"),
        (12, 26, "Santo Stefano"),
    ]
    events = [
        {
            "date": date(year, month, day).isoformat(),
            "name": name,
            "event_type": "holiday",
            "impact_level": "high",
            "description": "Festivita nazionale italiana",
            "source": "italian_holidays",
        }
        for month, day, name in fixed
    ]
    easter = _easter_sunday(year)
    events.extend([
        {
            "date": easter.isoformat(),
            "name": "Pasqua",
            "event_type": "holiday",
            "impact_level": "high",
            "description": "Festivita nazionale italiana",
            "source": "italian_holidays",
        },
        {
            "date": (easter + timedelta(days=1)).isoformat(),
            "name": "Lunedi dell'Angelo",
            "event_type": "holiday",
            "impact_level": "high",
            "description": "Festivita nazionale italiana",
            "source": "italian_holidays",
        },
    ])
    return events


def _geohash(latitude: float, longitude: float, precision: int = 6) -> str:
    """Encode coordinates without a new runtime dependency."""
    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    bits = [16, 8, 4, 2, 1]
    result = []
    bit = 0
    value = 0
    even = True

    while len(result) < precision:
        current = lon_range if even else lat_range
        midpoint = (current[0] + current[1]) / 2
        coordinate = longitude if even else latitude
        if coordinate >= midpoint:
            value |= bits[bit]
            current[0] = midpoint
        else:
            current[1] = midpoint
        even = not even
        if bit < 4:
            bit += 1
        else:
            result.append(_GEOHASH_ALPHABET[value])
            bit = 0
            value = 0
    return "".join(result)


def _event_category(name: str, classifications: list[dict]) -> str:
    text = " ".join([
        name or "",
        *[
            str((item.get("segment") or {}).get("name") or "")
            + " "
            + str((item.get("genre") or {}).get("name") or "")
            + " "
            + str((item.get("subGenre") or {}).get("name") or "")
            for item in classifications
        ],
    ]).lower()
    if any(term in text for term in ("fiera", "fair", "expo", "exhibition", "salone")):
        return "fair"
    if any(term in text for term in ("festival", "comics", "carnival", "carnevale")):
        return "festival"
    if any(term in text for term in ("concert", "music", "pop", "rock", "opera")):
        return "concert"
    if any(term in text for term in ("sport", "soccer", "football", "calcio", "race", "marathon")):
        return "sports"
    if any(term in text for term in ("conference", "congress", "business", "convention")):
        return "conference"
    return "local_event"


def _impact_for_category(category: str) -> str:
    if category in {"festival", "fair", "conference"}:
        return "high"
    if category in {"concert", "sports"}:
        return "medium"
    return "low"


class FreeEventProvider:
    """Automatic provider based on public holiday and Ticketmaster signals."""

    name = "free_events"

    def __init__(
        self,
        ticketmaster_api_key: Optional[str] = None,
        country_code: Optional[str] = None,
        radius_km: Optional[int] = None,
        timeout_seconds: float = 6.0,
    ) -> None:
        self.ticketmaster_api_key = (
            ticketmaster_api_key
            if ticketmaster_api_key is not None
            else os.getenv("TICKETMASTER_API_KEY", "").strip()
        )
        self.country_code = (country_code or os.getenv("PRICEPILOT_EVENTS_COUNTRY_CODE", "IT")).upper()
        raw_radius = radius_km if radius_km is not None else os.getenv("PRICEPILOT_EVENTS_RADIUS_KM", "40")
        try:
            self.radius_km = max(5, min(int(raw_radius), 150))
        except (TypeError, ValueError):
            self.radius_km = 40
        self.timeout_seconds = timeout_seconds
        self._ticketmaster_cache: dict[tuple, list[dict]] = {}

    def event_for_property(
        self,
        *,
        prop: dict,
        target_date: date,
        account_id: int = 1,
    ) -> Optional[dict]:
        events = self.events_for_property(prop=prop, target_date=target_date, account_id=account_id)
        return events[0] if events else None

    def event_for_date(self, target_date: date) -> Optional[dict]:
        """Backward-compatible lookup with national signals only."""
        events = [event for event in _italian_holidays(target_date.year) if event["date"] == target_date.isoformat()]
        return events[0] if events else None

    def events_for_property(
        self,
        *,
        prop: dict,
        target_date: date,
        account_id: int = 1,
    ) -> list[dict]:
        events = [
            event for event in _italian_holidays(target_date.year)
            if event["date"] == target_date.isoformat()
        ]
        events.extend(self._ticketmaster_events(prop, target_date))
        return sorted(
            events,
            key=lambda event: (
                -_IMPACT_PRIORITY.get(str(event.get("impact_level", "")).lower(), 0),
                float(event.get("distance_km") or 9999),
                str(event.get("name") or ""),
            ),
        )

    def event_to_string(self, event: Optional[dict]) -> str:
        if not event:
            return "none"
        return str(event.get("event_type") or "local_event").strip().lower()

    def event_label(self, event: Optional[dict]) -> str:
        if not event:
            return ""
        name = str(event.get("name") or event.get("event_type") or "Evento locale")
        category = str(event.get("event_type") or "event").replace("_", " ")
        distance = event.get("distance_km")
        suffix = f", {float(distance):.0f} km" if distance not in (None, "") else ""
        return f"{name} ({category}{suffix})"

    def _ticketmaster_events(self, prop: dict, target_date: date) -> list[dict]:
        if not self.ticketmaster_api_key:
            return []

        latitude = prop.get("latitude")
        longitude = prop.get("longitude")
        city = str(prop.get("city") or "").strip()
        if latitude in (None, "") or longitude in (None, ""):
            if not city:
                return []
            location_key = ("city", city.lower())
        else:
            try:
                latitude = float(latitude)
                longitude = float(longitude)
            except (TypeError, ValueError):
                return []
            location_key = ("geo", round(latitude, 3), round(longitude, 3))

        cache_key = (target_date.isoformat(), location_key, self.country_code, self.radius_km)
        if cache_key in self._ticketmaster_cache:
            return list(self._ticketmaster_cache[cache_key])

        params = {
            "apikey": self.ticketmaster_api_key,
            "countryCode": self.country_code,
            "startDateTime": f"{target_date.isoformat()}T00:00:00Z",
            "endDateTime": f"{target_date.isoformat()}T23:59:59Z",
            "size": 50,
            "sort": "relevance,desc",
        }
        if location_key[0] == "geo":
            params.update({
                "geoPoint": _geohash(latitude, longitude),
                "radius": self.radius_km,
                "unit": "km",
            })
        else:
            params["city"] = city

        request = Request(
            "https://app.ticketmaster.com/discovery/v2/events.json?" + urlencode(params),
            headers={"Accept": "application/json", "User-Agent": "PricePilot/1.0"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (URLError, TimeoutError, ValueError, OSError):
            self._ticketmaster_cache[cache_key] = []
            return []

        parsed = []
        for item in (payload.get("_embedded") or {}).get("events") or []:
            name = str(item.get("name") or "Evento locale").strip()
            category = _event_category(name, item.get("classifications") or [])
            venue = ((item.get("_embedded") or {}).get("venues") or [{}])[0]
            parsed.append({
                "date": target_date.isoformat(),
                "name": name,
                "event_type": category,
                "impact_level": _impact_for_category(category),
                "description": "Evento pubblico rilevato vicino alla proprieta",
                "distance_km": item.get("distance"),
                "venue": venue.get("name") or "",
                "source": "ticketmaster",
                "external_id": item.get("id") or "",
                "url": item.get("url") or "",
            })
        self._ticketmaster_cache[cache_key] = parsed
        return list(parsed)
