"""Server-owned production allowlist; never enumerate unapproved tenants.

Adding a future property requires an explicit reviewed allowlist entry. No
credentials or pricing rules belong here. Missing/mismatched setup fails closed
before inventory reads, operation runs, decisions or notifications.
"""
import json
from pathlib import Path

SCOPE_FILE = Path(__file__).resolve().parents[2] / 'config' / 'production_scheduler_scope.json'


def scheduled_properties():
    from pricepilot.core.database import get_property
    from pricepilot.services.operational_store import get_connection, get_calendar_policy

    entries = json.loads(SCOPE_FILE.read_text(encoding='utf-8')).get('properties')
    if not isinstance(entries, list) or not entries:
        raise ValueError('Perimetro scheduler assente: nessun immobile autorizzato.')
    selected, seen = {}, set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'account_id', 'property_id', 'beds24_property_id', 'room_id'}:
            raise ValueError('Perimetro scheduler non valido.')
        if any(type(value) is not int or value <= 0 for value in entry.values()):
            raise ValueError('Identificativi scheduler non validi.')
        account, prop_id = entry['account_id'], entry['property_id']
        if (account, prop_id) in seen:
            raise ValueError('Immobile scheduler duplicato.')
        seen.add((account, prop_id))
        prop = get_property(prop_id, account_id=account)
        if not prop or (prop.get('account_id'), prop.get('id')) != (account, prop_id):
            raise ValueError('Immobile scheduler fuori account autorizzato.')
        mapping = get_connection(account, prop_id) or {}
        policy = get_calendar_policy(account, prop_id) or {}
        if (mapping.get('provider') != 'beds24' or mapping.get('enabled') is not True
                or any(mapping.get(key) != entry[key] for key in ('account_id', 'property_id', 'beds24_property_id', 'room_id'))
                or policy.get('enabled') is not True):
            raise ValueError('Mapping o policy scheduler non operativi nel perimetro autorizzato.')
        selected.setdefault(account, []).append(prop)
    return selected
