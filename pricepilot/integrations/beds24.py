"""Beds24 API V2 adapter, implemented against docs/beds24-apiV2.yaml.

No network activity at import. Credentials are referenced per connection.
Writes only change the selected price slot, NEVER availability or restrictions.
Beds24 read-back confirms its calendar, not downstream OTA propagation.
"""
from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import time

import httpx

from pricepilot.core.config import BASE_DIR
from pricepilot.providers.contracts import ChannelUpdateResult

BASE_URL = "https://beds24.com/api/v2"


class Beds24Error(RuntimeError):
    pass


def _validated_mapping(value, account_id, property_id):
    if not isinstance(value, dict) or (value.get('account_id'), value.get('property_id')) != (account_id, property_id):
        raise Beds24Error("Connessione Beds24 fuori dal perimetro richiesto.")
    if value.get('provider') != 'beds24' or value.get('enabled') is not True:
        raise Beds24Error("Connessione Beds24 assente o disabilitata.")
    for field in ('beds24_property_id', 'room_id', 'price_slot'):
        item = value.get(field)
        if type(item) is not int or item < 1 or (field == 'price_slot' and item > 16):
            raise Beds24Error("Mapping Beds24 non valido.")
    if value.get('currency') != 'EUR' or value.get('price_basis', 'unknown') not in {'unknown', 'accommodation_only'}:
        raise Beds24Error("Valuta o classificazione importi Beds24 non valida.")
    for field in ('token_env', 'refresh_token_env'):
        item = value.get(field, '')
        if item and (not isinstance(item, str) or not re.fullmatch(r'BEDS24_[A-Z0-9_]{1,80}', item)):
            raise Beds24Error("Riferimento credenziale Beds24 non valido.")
    return dict(value)


def load_mapping(account_id, property_id):
    # Explicit file overrides remain useful for offline fixtures/migrations.
    # Normal app configuration is account/property scoped in the database.
    if not os.getenv("PRICEPILOT_CONNECTIONS_FILE"):
        from pricepilot.services.operational_store import get_connection
        m = get_connection(account_id, property_id)
        if m is not None:
            return _validated_mapping(m, account_id, property_id)
    path = Path(os.getenv("PRICEPILOT_CONNECTIONS_FILE") or BASE_DIR / "data" / "connections.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        matches = [r for r in data["connections"] if r.get("provider") == "beds24"
                   and r.get("account_id") == account_id and r.get("property_id") == property_id]
        if len(matches) != 1:
            raise ValueError()
        m = matches[0]
        return _validated_mapping(m, account_id, property_id)
    except (OSError, ValueError, KeyError, TypeError, Beds24Error):
        raise Beds24Error("Connessione Beds24 assente, disabilitata o mapping non valido.") from None


class Beds24Client:
    def __init__(self, *, token="", refresh_token="", transport=None):
        self._token = token
        self._refresh = refresh_token
        self._expires = 0 if not token else float("inf")
        self._http = httpx.Client(base_url=BASE_URL, timeout=20, follow_redirects=False,
                                  trust_env=False, transport=transport)

    def close(self):
        self._http.close()

    def _request(self, method, path, **kwargs):
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError:
            # A write timeout has unknown outcome: never automatically replay it.
            raise Beds24Error("Beds24 non raggiungibile; per una scrittura verificare l'esito prima di riprovare.") from None
        if response.status_code == 429:
            raise Beds24Error("Limite API Beds24 raggiunto: acquisizione sospesa, riprovare dopo il reset del provider.")
        if not 200 <= response.status_code < 300:
            raise Beds24Error(f"Beds24 HTTP {response.status_code}; nessuna risposta riservata esposta.")
        try:
            payload = response.json()
        except ValueError:
            raise Beds24Error("Risposta Beds24 non JSON.") from None
        if isinstance(payload, dict) and payload.get("success") is False:
            raise Beds24Error("Beds24 ha rifiutato la richiesta.")
        return payload

    def _headers(self):
        if not self._token or time.monotonic() >= self._expires:
            if not self._refresh:
                raise Beds24Error("Credenziale Beds24 non configurata.")
            reply = self._request("GET", "/authentication/token", headers={"refreshToken": self._refresh})
            try:
                self._token = reply["token"]
                lifetime = int(reply["expiresIn"])
                if not self._token or lifetime <= 60:
                    raise ValueError()
                self._expires = time.monotonic() + lifetime - 60
            except (KeyError, TypeError, ValueError):
                raise Beds24Error("Risposta autenticazione Beds24 non valida.") from None
        return {"token": self._token}

    def _pages(self, path, params):
        result = []
        for page in range(1, 101):
            reply = self._request("GET", path, headers=self._headers(), params={**params, "page": page})
            if not isinstance(reply, dict) or not isinstance(reply.get("data"), list):
                raise Beds24Error("Risposta Beds24 incompleta.")
            pages = reply.get('pages', {})
            has_next = pages.get('nextPageExists', False) if isinstance(pages, dict) else None
            if type(has_next) is not bool:
                raise Beds24Error("Metadati paginazione Beds24 non validi.")
            result.extend(reply["data"])
            if not has_next:
                return result
        raise Beds24Error("Paginazione Beds24 incompleta: nessun dato parziale dichiarato completo.")

    def calendar(self, mapping, start, end):
        rows = self._pages("/inventory/rooms/calendar", {
            "propertyId": mapping["beds24_property_id"], "roomId": mapping["room_id"],
            "startDate": start.isoformat(), "endDate": end.isoformat(),
            "includePrices": "true", "includeNumAvail": "true", "includeOverride": "true", "includeMinStay": "true"})
        if any(r.get("roomId") != mapping["room_id"] or r.get("propertyId") != mapping["beds24_property_id"] for r in rows):
            raise Beds24Error("Beds24 ha restituito un immobile diverso dal mapping.")
        return rows

    def discover_price_slot(self, mapping, expected_rule_name):
        """Read the room's named Beds24 price rules; never changes Beds24."""
        property_id, room_id = mapping.get("beds24_property_id"), mapping.get("room_id")
        if type(property_id) is not int or type(room_id) is not int or property_id < 1 or room_id < 1:
            raise Beds24Error("Property ID o Room ID Beds24 non validi.")
        rooms = self._pages("/properties/rooms", {
            "propertyId": property_id, "id": room_id, "includePriceRules": "true"})
        matches = [room for room in rooms if room.get("propertyId") == property_id and room.get("id") == room_id]
        if len(matches) != 1:
            raise Beds24Error("Beds24 non ha confermato esattamente l'alloggio richiesto.")
        rules = [rule for rule in (matches[0].get("priceRules") or [])
                 if rule.get("name") == expected_rule_name]
        if len(rules) != 1 or type(rules[0].get("id")) is not int or not 1 <= rules[0]["id"] <= 16:
            raise Beds24Error("Piano tariffario Beds24 assente o ambiguo: nessun slot salvato.")
        return rules[0]["id"]

    def bookings(self, mapping, start, end):
        rows = self._pages("/bookings", {"propertyId": mapping["beds24_property_id"],
            "roomId": mapping["room_id"], "arrivalTo": end.isoformat(), "departureFrom": start.isoformat()})
        if any(r.get("roomId") != mapping["room_id"] or r.get("propertyId") != mapping["beds24_property_id"] for r in rows):
            raise Beds24Error("Prenotazioni Beds24 fuori dal mapping richiesto.")
        # Return only operational fields: no guest contacts, payment details or messages.
        fields = {"id", "propertyId", "roomId", "roomQty", "status", "arrival", "departure",
                  "bookingTime", "modifiedTime", "channel", "price", "tax", "commission"}
        return [{k: v for k, v in r.items() if k in fields} for r in rows]

    def current_day(self, mapping, day):
        hits = []
        for room in self.calendar(mapping, day, day):
            for row in room.get("calendar", []):
                if date.fromisoformat(row["from"]) <= day <= date.fromisoformat(row.get("to") or row["from"]):
                    hits.append(row)
        if len(hits) != 1:
            raise Beds24Error("Tariffa/calendario Beds24 assente o ambiguo.")
        return hits[0]

    def set_price(self, mapping, day, price):
        value = Decimal(str(price))
        if not value.is_finite() or value <= 0 or value != value.quantize(Decimal(".01")):
            raise Beds24Error("Prezzo non valido.")
        before = self.current_day(mapping, day)
        if before.get("numAvail") != 1 or before.get("override", "none") != "none":
            raise Beds24Error("Data non liberamente vendibile: nessun aggiornamento inviato.")
        slot = f"price{mapping['price_slot']}"
        body = [{"roomId": mapping["room_id"], "calendar": [{"from": day.isoformat(),
                 "to": day.isoformat(), slot: float(value)}]}]
        reply = self._request("POST", "/inventory/rooms/calendar", headers=self._headers(), json=body)
        if not isinstance(reply, list) or len(reply) != 1 or reply[0].get("success") is not True:
            raise Beds24Error("Scrittura Beds24 non confermata; riconciliare prima di ripetere.")
        after = self.current_day(mapping, day)
        try:
            confirmed = Decimal(str(after[slot])) == value
        except (KeyError, ValueError):
            confirmed = False
        if not confirmed:
            raise Beds24Error("Prezzo inviato ma rilettura Beds24 diversa: esito da riconciliare.")
        return {"confirmation_scope": "beds24_calendar", "downstream_ota_verified": False,
                "price_slot": slot, "price": str(value), "date": day.isoformat()}


class Beds24ChannelProvider:
    name = "beds24"

    def update_price(self, *, prop, new_price, target_date, min_nights=1):
        if os.getenv("PRICEPILOT_ALLOW_CHANNEL_WRITES") != "1":
            return ChannelUpdateResult(ok=False, platform=self.name, error="Invio prezzi disabilitato: abilitare solo dopo il collaudo.")
        client = None
        try:
            m = load_mapping(int(prop["account_id"]), int(prop["id"]))
            client = Beds24Client(token=os.getenv(m.get("token_env", ""), ""),
                                  refresh_token=os.getenv(m.get("refresh_token_env", ""), ""))
            result = client.set_price(m, target_date, new_price)
            return ChannelUpdateResult(ok=True, platform=self.name, listing_id=str(m["room_id"]), is_real=True, raw=result)
        except Beds24Error as exc:
            return ChannelUpdateResult(ok=False, platform=self.name, is_real=True, error=str(exc))
        finally:
            if client:
                client.close()
