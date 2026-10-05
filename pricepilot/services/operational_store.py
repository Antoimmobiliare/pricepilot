"""Account/property scoped operational documents in the configured database.

SQLite schema is additive. Cloud uses the versioned operational_store.sql
migration and fails closed if unavailable; never falls back to local files.
One snapshot contains inventory and bookings, so their publication is atomic.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re

from pricepilot.core import database as db
from pricepilot.core.data_backend import is_supabase_primary, CloudDatabaseUnavailable


_CYCLE_READ_CACHE = ContextVar("pricepilot_operational_read_cache", default=None)


@contextmanager
def operational_read_cache():
    """Reuse one immutable operational snapshot during a pricing cycle.

    The cache is context-local, so concurrent accounts/requests cannot share
    documents. Writes refresh the matching entry and the cache is discarded at
    the end of the context.
    """
    token = _CYCLE_READ_CACHE.set({})
    try:
        yield
    finally:
        _CYCLE_READ_CACHE.reset(token)


def _scope(account_id, property_id):
    if type(account_id) is not int or type(property_id) is not int or min(account_id, property_id) < 1:
        raise ValueError('Account e appartamento non validi.')
    cache = _CYCLE_READ_CACHE.get()
    scope_key = ("scope", account_id, property_id)
    if cache is not None and cache.get(scope_key) is True:
        return
    if is_supabase_primary():
        from pricepilot.services.supabase_primary import _client
        try:
            rows = (_client().table('properties').select('local_id')
                    .eq('account_id', account_id).eq('local_id', property_id)
                    .limit(2).execute().data)
        except Exception:
            raise CloudDatabaseUnavailable(
                'Impossibile verificare il perimetro account/appartamento nel database cloud.'
            ) from None
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError('Appartamento non appartenente all’account.')
        if cache is not None:
            cache[scope_key] = True
        return
    with db.get_conn() as conn:
        row = conn.execute(
            'SELECT 1 FROM properties WHERE id=? AND account_id=? LIMIT 1',
            (property_id, account_id),
        ).fetchone()
    if row is None:
        raise ValueError('Appartamento non appartenente all’account.')
    if cache is not None:
        cache[scope_key] = True


def _table(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS operational_documents (
        account_id INTEGER NOT NULL, property_id INTEGER NOT NULL,
        kind TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(account_id, property_id, kind))''')
    # Additive guards also protect callers that bypass this module.
    conn.execute('''CREATE TRIGGER IF NOT EXISTS operational_documents_scope_insert
        BEFORE INSERT ON operational_documents
        WHEN NOT EXISTS (SELECT 1 FROM properties p
                         WHERE p.id=NEW.property_id AND p.account_id=NEW.account_id)
        BEGIN SELECT RAISE(ABORT, 'operational document outside property scope'); END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS operational_documents_scope_update
        BEFORE UPDATE ON operational_documents
        WHEN NOT EXISTS (SELECT 1 FROM properties p
                         WHERE p.id=NEW.property_id AND p.account_id=NEW.account_id)
        BEGIN SELECT RAISE(ABORT, 'operational document outside property scope'); END''')


def _get(account_id, property_id, kind):
    _scope(account_id, property_id)
    cache = _CYCLE_READ_CACHE.get()
    cache_key = ("document", account_id, property_id, kind)
    if cache is not None and cache_key in cache:
        return deepcopy(cache[cache_key])
    if is_supabase_primary():
        from pricepilot.services.supabase_primary import _client
        try:
            rows = (_client().table('operational_documents').select('payload')
                    .eq('account_id', account_id).eq('property_id', property_id)
                    .eq('kind', kind).limit(2).execute().data)
            if not isinstance(rows, list) or len(rows) > 1:
                raise ValueError()
            payload = rows[0]['payload'] if rows else None
            if isinstance(payload, str):
                payload = json.loads(payload)
            if payload is not None and not isinstance(payload, dict):
                raise ValueError()
            if payload is not None and (payload.get('account_id'), payload.get('property_id')) != (account_id, property_id):
                raise ValueError()
            if cache is not None:
                cache[cache_key] = deepcopy(payload)
            return payload
        except Exception:
            raise CloudDatabaseUnavailable('Archivio operativo cloud non disponibile: applicare operational_store.sql e verificare la connessione.') from None
    with db.get_conn() as conn:
        _table(conn)
        row = conn.execute('SELECT payload FROM operational_documents WHERE account_id=? AND property_id=? AND kind=?', (account_id, property_id, kind)).fetchone()
        payload = json.loads(row[0]) if row else None
        if payload is not None and (payload.get('account_id'), payload.get('property_id')) != (account_id, property_id):
            raise ValueError('Documento operativo fuori account/appartamento.')
        if cache is not None:
            cache[cache_key] = deepcopy(payload)
        return payload


def _save(account_id, property_id, kind, payload):
    _scope(account_id, property_id)
    # Round trip ensures the same JSON contract on both backends, rejecting NaN.
    encoded = json.dumps(payload, allow_nan=False, ensure_ascii=False)
    row = dict(account_id=account_id, property_id=property_id, kind=kind,
               payload=json.loads(encoded), updated_at=datetime.now(timezone.utc).isoformat())
    if is_supabase_primary():
        from pricepilot.services.supabase_primary import _client
        try:
            client = _client()
            client.table('operational_documents').upsert(
                row, on_conflict='account_id,property_id,kind').execute()
            check = (client.table('operational_documents').select('payload')
                     .eq('account_id', account_id).eq('property_id', property_id)
                     .eq('kind', kind).limit(2).execute().data)
            if not isinstance(check, list) or len(check) != 1:
                raise ValueError()
            saved = check[0].get('payload')
            if isinstance(saved, str):
                saved = json.loads(saved)
            if saved != row['payload']:
                raise ValueError()
        except Exception:
            raise CloudDatabaseUnavailable('Salvataggio cloud non riuscito: nessun ripiego locale.') from None
    else:
        with db.get_conn() as conn:
            _table(conn)
            conn.execute('''INSERT INTO operational_documents VALUES (?,?,?,?,?)
                ON CONFLICT(account_id,property_id,kind) DO UPDATE SET
                payload=excluded.payload,updated_at=excluded.updated_at''',
                (account_id, property_id, kind, encoded, row['updated_at']))
    cache = _CYCLE_READ_CACHE.get()
    if cache is not None:
        cache[("scope", account_id, property_id)] = True
        cache[("document", account_id, property_id, kind)] = deepcopy(row['payload'])
    return row['payload']


def get_calendar_policy(account_id, property_id):
    return _get(account_id, property_id, 'calendar_policy')


def save_calendar_policy(account_id, property_id, policy):
    from pricepilot.engine.calendar_pricing import validate_policy
    p = dict(policy, account_id=account_id, property_id=property_id)
    validate_policy(p)
    return _save(account_id, property_id, 'calendar_policy', p)


def get_connection(account_id, property_id):
    return _get(account_id, property_id, 'connection')


def save_connection(account_id, property_id, mapping):
    allowed = {'provider', 'enabled', 'beds24_property_id', 'room_id', 'price_slot',
               'currency', 'token_env', 'refresh_token_env', 'price_basis', 'account_id', 'property_id'}
    if set(mapping) - allowed:
        raise ValueError('Campi connessione non ammessi: le credenziali non vanno salvate nel database.')
    m = dict(mapping, account_id=account_id, property_id=property_id)
    if m.get('provider') != 'beds24' or type(m.get('enabled')) is not bool:
        raise ValueError('Provider o stato connessione non valido.')
    if m.get('currency', 'EUR') != 'EUR' or m.get('price_basis', 'unknown') not in {'unknown', 'accommodation_only'}:
        raise ValueError('Valuta o classificazione importi non valida.')
    m.setdefault('currency', 'EUR'); m.setdefault('price_basis', 'unknown')
    for name in ('token_env', 'refresh_token_env'):
        value = m.get(name, '')
        if value and not re.fullmatch(r'BEDS24_[A-Z0-9_]{1,80}', value):
            raise ValueError('Indicare solo il nome BEDS24_... della variabile segreta.')
    for key in ('beds24_property_id', 'room_id', 'price_slot'):
        value = m.get(key)
        if m['enabled'] or value is not None:
            if type(value) is not int or not 1 <= value <= (16 if key == 'price_slot' else 2**53-1):
                raise ValueError('Identificativo Beds24 o slot prezzo non valido.')
    return _save(account_id, property_id, 'connection', m)


def save_snapshot(account_id, property_id, inventory, reservations, start, end, observed_at):
    if end <= start:
        raise ValueError('Finestra snapshot non valida.')
    if not isinstance(inventory, list) or not isinstance(reservations, list) or any(
            not isinstance(r, dict) for r in inventory + reservations):
        raise ValueError('Contenuto snapshot non valido.')
    if any((r.get('account_id'), r.get('property_id')) != (account_id, property_id) for r in inventory + reservations):
        raise ValueError('Snapshot fuori account/appartamento.')
    try:
        observed = datetime.fromisoformat(str(observed_at).replace('Z', '+00:00'))
        if observed.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Orario snapshot privo di timezone.') from None
    days = [r['date'] for r in inventory]
    expected = {(start + timedelta(days=i)).isoformat() for i in range((end-start).days)}
    if len(set(days)) != len(days) or set(days) != expected:
        raise ValueError('Snapshot incompleto o date duplicate.')
    allowed_states = {'open', 'booked', 'owner_blocked', 'maintenance_blocked', 'unavailable'}
    for row in inventory:
        if row.get('state') not in allowed_states or row.get('observed_at') != observed_at:
            raise ValueError('Riga inventario non classificata o non coerente con lo snapshot.')
    seen_bookings = set()
    for reservation in reservations:
        booking_id = reservation.get('booking_id')
        if not isinstance(booking_id, str) or not booking_id or booking_id in seen_bookings:
            raise ValueError('Prenotazione senza identificativo univoco.')
        seen_bookings.add(booking_id)
        try:
            arrival = date.fromisoformat(reservation['arrival'])
            departure = date.fromisoformat(reservation['departure'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('Date prenotazione non valide.') from None
        if departure <= arrival or reservation.get('status') not in {
                'confirmed', 'new', 'cancelled', 'black', 'request', 'inquiry'}:
            raise ValueError('Prenotazione non classificata.')
        created_at = reservation.get('created_at')
        if created_at is not None:
            try:
                created = datetime.fromisoformat(str(created_at).replace('Z', '+00:00'))
                if created.tzinfo is None or created > observed:
                    raise ValueError()
            except (TypeError, ValueError):
                raise ValueError('Data di creazione prenotazione non verificabile.') from None
        for field in ('source_total', 'source_tax', 'source_commission'):
            value = reservation.get(field)
            if value is not None:
                try:
                    number = Decimal(str(value))
                except (InvalidOperation, TypeError):
                    raise ValueError('Componente economica della prenotazione non numerica.') from None
                if not number.is_finite() or number < 0:
                    raise ValueError('Componente economica della prenotazione non valida.')
        basis = reservation.get('amount_basis', 'unknown')
        accommodation = reservation.get('accommodation_total')
        if basis not in {'unknown', 'accommodation_only'}:
            raise ValueError('Base economica della prenotazione non valida.')
        if basis != 'accommodation_only' and accommodation is not None:
            raise ValueError('Ricavo alloggio presente senza classificazione verificata.')
        if basis == 'accommodation_only':
            try:
                accommodation_value = Decimal(str(accommodation))
                source_value = Decimal(str(reservation.get('source_total')))
            except (InvalidOperation, TypeError):
                raise ValueError('Ricavo alloggio non numerico.') from None
            if accommodation is None or accommodation_value != source_value:
                raise ValueError('Ricavo alloggio incompleto o diverso dal totale verificato.')
            if not accommodation_value.is_finite() or accommodation_value < 0:
                raise ValueError('Ricavo alloggio non valido.')
    return _save(account_id, property_id, 'snapshot', dict(
        account_id=account_id, property_id=property_id, inventory=inventory,
        reservations=reservations, window_start=start.isoformat(), window_end=end.isoformat(),
        observed_at=observed_at, source='beds24', valid=True))


def invalidate_snapshot(account_id, property_id):
    existing = _get(account_id, property_id, 'snapshot')
    if existing:
        existing['valid'] = False
        _save(account_id, property_id, 'snapshot', existing)


def get_snapshot(account_id, property_id):
    return _get(account_id, property_id, 'snapshot')


def get_inventory_rows(account_id, property_id, start, end):
    snapshot = get_snapshot(account_id, property_id)
    if not snapshot or not snapshot.get('valid'):
        return []
    return [r for r in snapshot['inventory'] if start.isoformat() <= r['date'] < end.isoformat()]


def get_reservation_metrics(account_id, property_id, start, end, now=None):
    """Stay-date metrics [start,end); pickup is gross currently-active nights.

    Period allocation is proportional across a stay (not observed nightly ADR).
    Any unclassified accommodation amount makes monetary aggregates unknown.
    Owner/maintenance/unavailable dates are excluded from sellable nights.
    """
    now = now or datetime.now(timezone.utc)
    snapshot = get_snapshot(account_id, property_id)
    result = dict(adr=None, revpar=None, occupancy=None, booked_nights=0,
        available_nights=0, accommodation_revenue=None, amount_coverage=None,
        pickup_7d_nights=None, pickup_7d_revenue=None, observed_at=None, complete=False,
        pickup_definition='gross_active_booked_nights_created_last_7_days',
        revenue_allocation='proportional_stay_allocation')
    if end <= start:
        raise ValueError('Finestra metriche non valida.')
    if not snapshot or not snapshot.get('valid'):
        return result
    try:
        observed = datetime.fromisoformat(str(snapshot['observed_at']).replace('Z', '+00:00'))
        if observed.tzinfo is None or now.tzinfo is None or not 0 <= (now-observed).total_seconds() <= 6*3600:
            return result
    except (KeyError, TypeError, ValueError):
        return result
    rows = [r for r in snapshot['inventory'] if start.isoformat() <= r['date'] < end.isoformat()]
    result['observed_at'] = snapshot['observed_at']
    if len(rows) != (end-start).days:
        return result
    available = sum(r['state'] in {'open', 'booked'} for r in rows)
    booked = sum(r['state'] == 'booked' for r in rows)
    revenue, known, pickup, pickup_known = Decimal(0), 0, 0, True
    pickup_revenue, pickup_revenue_known = Decimal(0), True
    active = [b for b in snapshot['reservations'] if b['status'] in {'confirmed', 'new'}]
    accounted = 0
    for b in active:
        arrival, departure = date.fromisoformat(b['arrival']), date.fromisoformat(b['departure'])
        nights = max(0, (min(departure, end)-max(arrival, start)).days)
        if not nights:
            continue
        accounted += nights
        amount = b.get('accommodation_total')
        amount_verified = b.get('amount_basis') == 'accommodation_only' and amount is not None
        if amount_verified:
            revenue += Decimal(str(amount)) * nights / (departure-arrival).days
            known += nights
        stamp = b.get('created_at')
        if stamp is None:
            pickup_known = False
            pickup_revenue_known = False
        else:
            created = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
            if created.tzinfo is None:
                pickup_known = False
                pickup_revenue_known = False
            elif now-timedelta(days=7) <= created <= now:
                pickup += nights
                if amount_verified:
                    pickup_revenue += Decimal(str(amount)) * nights / (departure-arrival).days
                else:
                    pickup_revenue_known = False
    if accounted != booked:
        return result
    result.update(complete=True, available_nights=available, booked_nights=booked,
        occupancy=booked/available if available else None,
        amount_coverage=known/booked if booked else 1.0,
        pickup_7d_nights=pickup if pickup_known else None,
        pickup_7d_revenue=round(float(pickup_revenue), 2)
            if pickup_known and pickup_revenue_known else None)
    if known == booked:
        result.update(accommodation_revenue=round(float(revenue),2),
            adr=round(float(revenue/booked),2) if booked else None,
            revpar=round(float(revenue/available),2) if available else None)
    return result
