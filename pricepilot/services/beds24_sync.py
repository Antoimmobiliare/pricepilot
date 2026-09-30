"""Acquire a whole-property Beds24 snapshot before a pricing horizon.

Snapshots replace only the requested tenant/property. Conflicts fail closed;
never infer occupancy from unavailable dates alone. Atomic file publication is
for a SINGLE HOST prelaunch deployment; distributed storage remains a launch gate.
"""
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile

from pricepilot.core.config import BASE_DIR
from pricepilot.integrations.beds24 import Beds24Client, Beds24Error, load_mapping


def normalize_reservations(mapping, bookings, observed_at):
    """Keep provider amounts distinct from accommodation revenue.

    Beds24's generic price field has no guaranteed fee/tax breakdown. It becomes
    ADR input only after the owner explicitly verifies accommodation_only in
    the connection. Never infer this from price minus tax or commission.
    """
    from pricepilot.providers.observations import money
    try:
        observed = datetime.fromisoformat(str(observed_at).replace('Z', '+00:00'))
        if observed.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise Beds24Error('Orario acquisizione prenotazioni non valido.') from None
    result, seen = [], set()
    for b in bookings:
        if (b.get('propertyId'), b.get('roomId')) != (mapping['beds24_property_id'], mapping['room_id']):
            raise Beds24Error('Prenotazione fuori dal mapping.')
        if type(b.get('id')) is not int or b['id'] <= 0 or b['id'] in seen:
            raise Beds24Error('Identificativo prenotazione non valido o duplicato.')
        seen.add(b['id'])
        if type(b.get('roomQty', 1)) is not int or b.get('roomQty', 1) != 1:
            raise Beds24Error('Prenotazione multi-unita: richiede un mapping distinto per appartamento.')
        if b.get('status') not in {'confirmed','new','cancelled','black','request','inquiry'}:
            raise Beds24Error('Stato prenotazione non riconosciuto.')
        try:
            arrival, departure = date.fromisoformat(b['arrival']), date.fromisoformat(b['departure'])
        except (KeyError, TypeError, ValueError):
            raise Beds24Error('Date prenotazione non valide.') from None
        if departure <= arrival:
            raise Beds24Error('Date prenotazione non valide.')
        created = None
        if b.get('bookingTime'):
            try:
                stamp = datetime.fromisoformat(b['bookingTime'].replace('Z','+00:00'))
                if stamp.tzinfo is not None and stamp <= observed:
                    created = stamp.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError):
                pass
        total = str(money(b['price'])) if b.get('price') is not None else None
        result.append(dict(account_id=mapping['account_id'], property_id=mapping['property_id'],
            source='beds24', source_reference=f"beds24:booking:{b['id']}", booking_id=str(b['id']),
            status=b['status'], arrival=arrival.isoformat(), departure=departure.isoformat(),
            created_at=created, booking_time_source=b.get('bookingTime'), observed_at=observed_at,
            channel=b.get('channel'), currency=mapping['currency'], source_total=total,
            source_tax=str(money(b['tax'])) if b.get('tax') is not None else None,
            source_commission=str(money(b['commission'])) if b.get('commission') is not None else None,
            accommodation_total=total if mapping.get('price_basis') == 'accommodation_only' else None,
            amount_basis=mapping.get('price_basis', 'unknown')))
    return result


def normalize_snapshot(mapping, calendar, bookings, start, end, observed_at):
    if end <= start:
        raise Beds24Error("Finestra inventario non valida.")
    by_day = {}
    for room in calendar:
        # Calendar records from Beds24 V2 do not contain propertyId. The GET
        # request is already property-scoped; roomId remains mandatory.
        if room.get("roomId") != mapping["room_id"]:
            raise Beds24Error("Calendario fuori dal mapping.")
        entries = room.get('calendar')
        if not isinstance(entries, list):
            raise Beds24Error('Calendario Beds24 incompleto.')
        for entry in entries:
            try:
                first = date.fromisoformat(entry["from"])
                last = date.fromisoformat(entry.get("to") or entry["from"])
            except (KeyError, TypeError, ValueError):
                raise Beds24Error('Data calendario Beds24 non valida.') from None
            if last < first:
                raise Beds24Error("Intervallo calendario non valido.")
            for i in range(max(0, (min(last + timedelta(days=1), end) - max(first, start)).days)):
                day = max(first, start) + timedelta(days=i)
                if day in by_day:
                    raise Beds24Error("Intervalli calendario sovrapposti.")
                by_day[day] = entry
    occupied, held, seen = {}, set(), set()
    for b in bookings:
        if b.get("roomId") != mapping["room_id"] or b.get("propertyId") != mapping["beds24_property_id"]:
            raise Beds24Error("Prenotazioni fuori dal mapping.")
        if not b.get("id") or isinstance(b.get("id"), bool):
            raise Beds24Error("Identificativo prenotazione mancante.")
        if b.get("id") in seen:
            raise Beds24Error("Prenotazione duplicata fra le pagine.")
        seen.add(b.get("id"))
        if type(b.get('roomQty', 1)) is not int or b.get('roomQty', 1) != 1:
            raise Beds24Error('Prenotazione multi-unita non compatibile con un appartamento intero.')
        if b.get("status") in {"cancelled", "inquiry"}:
            continue
        if b.get("status") not in {"confirmed", "new", "request", "black"}:
            raise Beds24Error("Stato prenotazione sconosciuto.")
        try:
            first, last = date.fromisoformat(b["arrival"]), date.fromisoformat(b["departure"])
        except (KeyError, TypeError, ValueError):
            raise Beds24Error('Date prenotazione non valide.') from None
        if last <= first:
            raise Beds24Error("Date prenotazione non valide.")
        for i in range(max(0, (min(last, end) - max(first, start)).days)):
            day = max(first, start) + timedelta(days=i)
            if b["status"] in {"confirmed", "new"}:
                if day in occupied:
                    raise Beds24Error("Overbooking: due prenotazioni sulla stessa notte.")
                occupied[day] = b["id"]
            else:
                held.add(day)
    rows = []
    slot = f"price{mapping['price_slot']}"
    for offset in range((end-start).days):
        day = start + timedelta(days=offset)
        c = by_day.get(day)
        if c is None or type(c.get("numAvail")) is not int or c["numAvail"] not in (0, 1):
            raise Beds24Error("Inventario mancante o non compatibile con un appartamento intero.")
        override = c.get('override', 'none')
        if override not in {'none', 'blackout', 'exception', 'noCheckIn', 'noCheckOut', 'noCheckInOrCheckOut'}:
            raise Beds24Error('Restrizione calendario Beds24 non riconosciuta.')
        if day in occupied:
            if c["numAvail"] != 0 or day in held or override == "blackout":
                raise Beds24Error("Conflitto fra calendario e prenotazioni.")
            state = "booked"
        elif day in held or override == "blackout" or c["numAvail"] == 0:
            # Unknown block reason is not a confirmed booking. Requests are
            # ambiguous and prevent a certified occupancy snapshot.
            if day in held:
                raise Beds24Error("Blocco/richiesta da classificare prima di calcolare occupancy.")
            state = "unavailable"
        else:
            state = "open"
        rows.append({"account_id": mapping["account_id"], "property_id": mapping["property_id"],
            "date": day.isoformat(), "state": state, "observed_at": observed_at,
            "source_reference": f"beds24:room:{mapping['room_id']}", "booking_id": occupied.get(day),
            "current_price": c.get(slot), "min_stay": c.get("minStay"),
            "arrival_restriction": override})
    return rows


def publish_snapshot(rows, mapping):
    if any((r.get("account_id"), r.get("property_id")) !=
           (mapping["account_id"], mapping["property_id"]) for r in rows):
        raise Beds24Error("Snapshot fuori dal mapping richiesto.")
    path = Path(os.getenv("PRICEPILOT_INVENTORY_FILE") or BASE_DIR / "data" / "inventory.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise Beds24Error("Sincronizzazione inventario gia in corso; un lock residuo richiede verifica.") from None
    temporary = None
    try:
        os.close(lock_fd)
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"schema_version":"pricepilot.inventory.v1","observations":[]}
        if old.get("schema_version") != "pricepilot.inventory.v1":
            raise Beds24Error("Formato inventario precedente non riconosciuto.")
        keep = [r for r in old["observations"] if (r.get("account_id"),r.get("property_id")) != (mapping["account_id"],mapping["property_id"])]
        content = json.dumps({"schema_version":"pricepilot.inventory.v1","observations":keep+rows},indent=2)
        with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=path.parent,delete=False) as f:
            temporary = Path(f.name); f.write(content); f.flush(); os.fsync(f.fileno())
        os.replace(temporary,path)
    finally:
        if temporary and temporary.exists(): temporary.unlink()
        lock.unlink()


def sync_property(account_id, property_id, start, horizon_days=90):
    from pricepilot.services.operational_store import save_snapshot, invalidate_snapshot
    mapping = load_mapping(account_id,property_id)
    end = start + timedelta(days=horizon_days+29)  # Last pricing date needs its complete 30-day occupancy window.
    # Retain recent stays for real stay-date metrics; no synthetic historical rates.
    acquisition_start = start - timedelta(days=90)
    # Previous snapshot cannot certify current availability after acquisition fails.
    invalidate_snapshot(account_id, property_id)
    client= Beds24Client(token=os.getenv(mapping.get("token_env",""),""),
                        refresh_token=os.getenv(mapping.get("refresh_token_env",""),""))
    try:
        try:
            cal=client.calendar(mapping,acquisition_start,end-timedelta(days=1))
        except Beds24Error as exc:
            raise Beds24Error(f"calendar_get: {exc}") from None
        try:
            bookings=client.bookings(mapping,acquisition_start,end)
        except Beds24Error as exc:
            raise Beds24Error(f"bookings_get: {exc}") from None
        observed_at=datetime.now(timezone.utc).isoformat()
        try:
            rows=normalize_snapshot(mapping,cal,bookings,acquisition_start,end,observed_at)
        except Beds24Error as exc:
            raise Beds24Error(f"calendar_parse/normalize: {exc}") from None
        except Exception:
            raise Beds24Error("calendar_parse/normalize: errore interno PricePilot.") from None
        try:
            reservations=normalize_reservations(mapping,bookings,observed_at)
        except Beds24Error as exc:
            raise Beds24Error(f"bookings_parse/normalize: {exc}") from None
        except Exception:
            raise Beds24Error("bookings_parse/normalize: errore interno PricePilot.") from None
    finally:
        client.close()
    # Persist observed current rates using the existing repository, preserving locks.
    from pricepilot.core.database import get_calendar_price, upsert_calendar_price
    from pricepilot.providers.observations import money
    # Validate the entire batch before the first database mutation.
    for row in rows:
        if row["current_price"] is not None:
            money(row["current_price"])
    for row in rows:
        if row["current_price"] is not None and money(row["current_price"]) > 0:
            existing=get_calendar_price(property_id,row["date"],account_id)
            if existing and existing.get("status")=="locked":
                continue
            upsert_calendar_price({**(existing or {}), "account_id":account_id,"property_id":property_id,"date":row["date"],
                "current_price":row["current_price"],"current_price_source":"beds24_observation",
                "status":(existing or {}).get("status") or "observed",
                "notes":(existing or {}).get("notes") or "Tariffa calendario Beds24; propagazione OTA non verificata."})
    save_snapshot(account_id,property_id,rows,reservations,acquisition_start,end,observed_at)
    # Optional compatibility export only when explicitly requested. Database is
    # the canonical source for application, scheduler and authenticated API.
    if os.getenv('PRICEPILOT_INVENTORY_FILE'):
        publish_snapshot(rows,mapping)
    return {"days":len(rows),"source":"beds24","observed_at":rows[0]["observed_at"] if rows else None}
