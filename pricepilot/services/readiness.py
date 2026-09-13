"""Readiness is evidence, not a percentage of profile fields completed."""
from datetime import date, datetime, timedelta, timezone
import os

from pricepilot.core.database import get_account, get_properties, get_telegram_link_by_property
from pricepilot.core.plans import get_plan
from pricepilot.services.operational_store import get_calendar_policy, get_connection, get_inventory_rows
from pricepilot.providers.observations import fresh_timestamp
from pricepilot.engine.calendar_pricing import validate_policy


def _check(key, label, ok, detail='', required=True):
    return dict(key=key, label=label, ok=bool(ok), detail=detail, required=required)


def property_readiness(account_id, property_id):
    props = get_properties(account_id=account_id)
    prop = next((p for p in props if p['id'] == property_id), None)
    if not prop:
        raise ValueError('Appartamento non appartenente all’account.')
    checks = []
    checks.append(_check('price_bounds', 'Limiti prezzo',
        0 < float(prop.get('min_price') or 0) < float(prop.get('max_price') or 0), 'Minimo e massimo per notte.'))
    policy = connection = None
    rows = []
    try:
        policy = get_calendar_policy(account_id, property_id)
        connection = get_connection(account_id, property_id)
        rows = get_inventory_rows(account_id, property_id, date.today(), date.today()+timedelta(days=30))
        storage_ok = True
    except (RuntimeError, ValueError):
        storage_ok = False
    checks.append(_check('storage', 'Archivio operativo', storage_ok, 'Database disponibile.' if storage_ok else 'Verifica database e migrazioni operative.'))
    policy_ok = bool(policy and policy.get('enabled'))
    if policy_ok:
        try:
            validate_policy(policy)
        except (KeyError, TypeError, ValueError):
            policy_ok = False
    checks.append(_check('policy', 'Strategie valide e abilitate', policy_ok,
                         'Configura tariffa di riferimento e regole in Proprietà.'))
    checks.append(_check('connection', 'Collegamento Beds24 abilitato', bool(connection and connection.get('enabled')),
                         'Configura gli ID in Integrazioni.'))
    credentials = connection and (
        os.getenv(connection.get('token_env', ''), '').strip()
        or os.getenv(connection.get('refresh_token_env', ''), '').strip()
    )
    checks.append(_check('credentials', 'Credenziali installate', bool(credentials), 'Presenza verificata; la validità richiede una lettura reale.'))
    inventory_ok = len(rows) == 30
    try:
        now = datetime.now(timezone.utc)
        for row in rows:
            fresh_timestamp(row.get('observed_at'), now)
    except (ValueError, TypeError):
        inventory_ok = False
    checks.append(_check('inventory', 'Calendario completo e aggiornato', inventory_ok,
        'Verificati 30 giorni con osservazioni entro 6 ore.' if inventory_ok else 'Acquisisci un calendario completo; dati assenti o scaduti non generano proposte.'))
    tg = get_telegram_link_by_property(property_id)
    linked = bool(tg and tg.get('active') and tg.get('chat_id'))
    checks.append(_check('telegram', 'Telegram collegato', linked, 'Collega il tuo account nella sezione Telegram.'))
    from pricepilot.services.telegram_bot import is_configured
    checks.append(_check('telegram_bot', 'Bot Telegram configurato', is_configured(), 'Credenziali del bot installate sul server.'))
    checks.append(_check('channel_writes', 'Invio prezzi abilitato', os.getenv('PRICEPILOT_ALLOW_CHANNEL_WRITES') == '1',
        'Da attivare solo dopo una prova reale di lettura e scrittura Beds24.', required=False))
    states = {c['key']: c['ok'] for c in checks}
    configured = all(states[k] for k in {'price_bounds', 'storage', 'policy'})
    analysis_ready = configured and states['connection'] and states['credentials'] and states['inventory']
    approval_ready = analysis_ready and states['telegram'] and states['telegram_bot']
    write_ready = approval_ready and states['channel_writes']
    return {'checks': checks, 'analysis_ready': analysis_ready,
            'approval_ready': approval_ready, 'write_ready': write_ready,
            'ready': write_ready, 'configured': configured,
            'observed_at': min((r['observed_at'] for r in rows), default=None)}


def account_readiness(account_id=1):
    account = get_account(account_id) or {}
    properties = get_properties(account_id=account_id)
    checks = [_check('properties', 'Almeno un appartamento', bool(properties), 'Aggiungi un appartamento in Proprietà.')]
    property_states = []
    for prop in properties:
        state = property_readiness(account_id, prop['id'])
        property_states.append(state)
        for check in state['checks']:
            checks.append({**check, 'key': f"property:{prop['id']}:{check['key']}", 'label': f"{prop['name']} — {check['label']}"})
    required = [c for c in checks if c['required']]
    blockers = [c for c in required if not c['ok']]
    warnings = [c for c in checks if not c['required'] and not c['ok']]
    return dict(account_id=account_id, plan=get_plan(account.get('plan')), checks=checks,
                blockers=blockers, warnings=warnings,
                configured=bool(properties) and all(s['configured'] for s in property_states),
                analysis_ready=bool(properties) and all(s['analysis_ready'] for s in property_states),
                approval_ready=bool(properties) and all(s['approval_ready'] for s in property_states),
                write_ready=bool(properties) and all(s['write_ready'] for s in property_states),
                ready=bool(properties) and all(s['write_ready'] for s in property_states),
                score=round(100*sum(c['ok'] for c in required)/len(required), 1) if required else 0.0)
