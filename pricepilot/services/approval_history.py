"""Persist confirmed Beds24 approvals; reconciliation never invokes a writer."""
import json
from datetime import date
from decimal import Decimal


def record_confirmed_approval(prop, decision, channel_result, applied_at):
    raw = channel_result.get('raw') or {}
    if (not channel_result.get('ok') or not channel_result.get('is_real')
            or channel_result.get('platform') != 'beds24'
            or raw.get('confirmation_scope') != 'beds24_calendar'
            or raw.get('date') != decision['date']
            or raw.get('price_slot') not in {f'price{i}' for i in range(1, 17)}
            or Decimal(str(raw.get('price'))) != Decimal(str(decision['new_price']))
            or int(prop['account_id']) != int(decision['account_id'])
            or int(prop['id']) != int(decision['property_id'])):
        raise ValueError('Approval history requires matching confirmed Beds24 readback.')
    from pricepilot.core.database import record_price_update
    return record_price_update(prop, {**channel_result, 'new_price': decision['new_price'],
                               'decision_log_id': decision['id'], 'applied_at': applied_at},
                               date.fromisoformat(decision['date']))


def reconcile_approval_history(log_id, account_id):
    from pricepilot.core.database import (get_decision_log_entry, get_property,
                                          get_audit_events, record_audit_event)
    row = get_decision_log_entry(log_id, account_id)
    if not row or not row.get('applied') or '[APPROVED_SYNCED]' not in row.get('decision', ''):
        raise ValueError('Decision has no confirmed applied approval.')
    prop = get_property(row['property_id'], account_id=account_id)
    audits = get_audit_events(limit=1000, account_id=account_id, property_id=row['property_id'])
    evidence = [r for r in audits if r['action'] == 'decision_approved'
                and str(r['entity_id']) == str(log_id) and r['status'] == 'applied']
    if len(evidence) != 1:
        raise ValueError('A unique confirmed approval audit is required.')
    audit = evidence[0]
    details = audit['details']
    details = json.loads(details) if isinstance(details, str) else details
    if details.get('applied') is not True:
        raise ValueError('Audit does not confirm application.')
    update_id = record_confirmed_approval(prop, row, details['channel_manager'], audit['timestamp'])
    # The economic history record is idempotent; each reconciliation invocation is auditable.
    record_audit_event(action='approval_history_reconciled', entity_type='decision_log',
                       entity_id=log_id, account_id=account_id, property_id=row['property_id'],
                       source='audit_reconciliation', status='ok',
                       details={'approval_audit_id': audit['id'], 'price_update_id': update_id,
                                'channel_write_performed': False})
    return update_id
