"""Real temporary SQLite + sanitized Beds24 fixtures. No real service calls."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from pricepilot.core import database as db
from pricepilot.core.config import CONFIG
from pricepilot.services import operational_store as store
from pricepilot.services.beds24_sync import normalize_reservations, normalize_snapshot, sync_property
from pricepilot.integrations.beds24 import Beds24Error, load_mapping
from pricepilot.providers.observations import ObservedInventoryProvider

DAY = date(2026,10,1)
NOW = datetime(2026,10,1,12,tzinfo=timezone.utc)


class _Result:
    def __init__(self, data): self.data = data


class _CloudQuery:
    def __init__(self, client, table):
        self.client, self.table, self.filters, self.mode, self.payload = client, table, {}, 'select', None
    def select(self, _columns): self.mode = 'select'; return self
    def eq(self, key, value): self.filters[key] = value; return self
    def limit(self, _value): return self
    def upsert(self, payload, on_conflict=None):
        self.mode, self.payload = 'upsert', dict(payload)
        self.client.conflict = on_conflict
        return self
    def execute(self):
        if self.client.fail:
            raise RuntimeError('synthetic cloud failure')
        if self.table == 'properties':
            match = self.filters == {'account_id': self.client.account_id,
                                     'local_id': self.client.property_id}
            return _Result([{'local_id': self.client.property_id}] if match else [])
        key = (self.filters.get('account_id'), self.filters.get('property_id'), self.filters.get('kind'))
        if self.mode == 'upsert':
            key = (self.payload['account_id'], self.payload['property_id'], self.payload['kind'])
            if not self.client.discard_write:
                self.client.documents[key] = self.payload['payload']
            return _Result([])
        payload = self.client.documents.get(key)
        return _Result([{'payload': payload}] if payload is not None else [])


class _CloudClient:
    def __init__(self, account_id, property_id, *, fail=False, discard_write=False):
        self.account_id, self.property_id = account_id, property_id
        self.fail, self.discard_write, self.documents, self.conflict = fail, discard_write, {}, None
    def table(self, name): return _CloudQuery(self, name)


class OperationalStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'PRICEPILOT_TESTING':'1', 'SUPABASE_URL':'',
            'SUPABASE_ANON_KEY':'', 'SUPABASE_SERVICE_ROLE_KEY':'',
            'PRICEPILOT_DATABASE_BACKEND':'sqlite'})
        self.env.start(); self.addCleanup(self.env.stop)
        old = CONFIG['db_path']; CONFIG['db_path'] = str(Path(self.tmp.name)/'store.db')
        self.addCleanup(lambda: CONFIG.update(db_path=old))
        db.init_db()
        self.account = db.create_account('Storage fixture', plan='plus')['id']
        self.foreign = db.create_account('Other fixture', plan='plus')['id']
        from pricepilot.services.property_service import create_property
        self.pid = create_property(dict(account_id=self.account,name='Synthetic',min_price=50,max_price=300,platform='airbnb'))['id']
        self.mapping = dict(provider='beds24',enabled=True,account_id=self.account,property_id=self.pid,
            beds24_property_id=10,room_id=20,price_slot=1,currency='EUR',token_env='BEDS24_FIXTURE_TOKEN')

    def booking(self, **extra):
        return dict(id=1,propertyId=10,roomId=20,roomQty=1,status='confirmed',arrival=DAY.isoformat(),
            departure=(DAY+timedelta(days=2)).isoformat(),price=240,tax=10,commission=30,
            bookingTime='2026-09-30T10:00:00Z',**extra)

    def snapshot(self, mapping=None, booking=None):
        m = mapping or self.mapping
        b = booking if booking is not None else self.booking()
        calendar = [dict(propertyId=10,roomId=20,calendar=[dict(
            **{'from':(DAY+timedelta(days=i)).isoformat()}, numAvail=0 if i<2 or i==3 else 1,
            price1=120,minStay=1) for i in range(5)])]
        rows = normalize_snapshot(m,calendar,[b],DAY,DAY+timedelta(days=5),NOW.isoformat())
        reservations = normalize_reservations(m,[b],NOW.isoformat())
        store.save_snapshot(self.account,self.pid,rows,reservations,DAY,DAY+timedelta(days=5),NOW.isoformat())

    def test_connection_persists_without_plaintext_secrets(self):
        store.save_connection(self.account,self.pid,self.mapping)
        self.assertEqual(load_mapping(self.account,self.pid)['room_id'],20)
        with self.assertRaises(ValueError):
            store.save_connection(self.account,self.pid,{**self.mapping,'token':'SYNTHETIC_SECRET'})
        with self.assertRaises(ValueError):
            store.save_connection(self.account,self.pid,{**self.mapping,'token_env':'SECRET_VALUE'})

    def test_cross_account_read_and_write_rejected(self):
        for fn,args in [(store.get_connection,()),(store.save_connection,(self.mapping,))]:
            with self.assertRaises(ValueError):
                fn(self.foreign,self.pid,*args)

    def test_disabled_connection_can_save_partial_setup(self):
        store.save_connection(self.account,self.pid,dict(provider='beds24',enabled=False))
        with self.assertRaises(Beds24Error):
            load_mapping(self.account,self.pid)

    def test_unknown_price_basis_does_not_invent_adr(self):
        self.snapshot()
        metric=store.get_reservation_metrics(self.account,self.pid,DAY,DAY+timedelta(days=5),now=NOW)
        self.assertTrue(metric['complete'])
        self.assertEqual(metric['booked_nights'],2)
        self.assertEqual(metric['available_nights'],4)
        self.assertEqual(metric['occupancy'],.5)
        self.assertIsNone(metric['adr']); self.assertIsNone(metric['revpar'])
        self.assertEqual(metric['amount_coverage'],0)
        self.assertEqual(metric['pickup_7d_nights'],2)

    def test_verified_accommodation_amount_and_partial_stay_allocation(self):
        self.snapshot(mapping={**self.mapping,'price_basis':'accommodation_only'})
        metric=store.get_reservation_metrics(self.account,self.pid,DAY+timedelta(days=1),DAY+timedelta(days=5),now=NOW)
        self.assertEqual(metric['accommodation_revenue'],120)
        self.assertEqual(metric['adr'],120)
        self.assertEqual(metric['revpar'],40)
        self.assertEqual(metric['pickup_7d_revenue'],120)

    def test_unknown_created_timezone_does_not_invent_pickup(self):
        b=self.booking(); b['bookingTime']='2026-09-30T10:00:00'
        self.snapshot(booking=b)
        metric=store.get_reservation_metrics(self.account,self.pid,DAY,DAY+timedelta(days=5),now=NOW)
        self.assertIsNone(metric['pickup_7d_nights'])

    def test_z_timestamp_is_valid_for_pickup(self):
        b=self.booking(); b['bookingTime']='2026-09-30T10:00:00Z'
        self.snapshot(booking=b)
        snapshot=store.get_snapshot(self.account,self.pid)
        snapshot['reservations'][0]['created_at']='2026-09-30T10:00:00Z'
        store.save_snapshot(self.account,self.pid,snapshot['inventory'],snapshot['reservations'],
                            DAY,DAY+timedelta(days=5),NOW.isoformat())
        metric=store.get_reservation_metrics(self.account,self.pid,DAY,DAY+timedelta(days=5),now=NOW)
        self.assertEqual(metric['pickup_7d_nights'],2)

    def test_maintenance_block_is_excluded_from_sellable_nights(self):
        b=self.booking()
        calendar = [dict(propertyId=10,roomId=20,calendar=[dict(
            **{'from':(DAY+timedelta(days=i)).isoformat()},numAvail=0 if i<2 else 1,
            price1=120,minStay=1) for i in range(5)])]
        rows=normalize_snapshot(self.mapping,calendar,[b],DAY,DAY+timedelta(days=5),NOW.isoformat())
        rows[-1]['state']='maintenance_blocked'
        reservations=normalize_reservations(self.mapping,[b],NOW.isoformat())
        store.save_snapshot(self.account,self.pid,rows,reservations,DAY,DAY+timedelta(days=5),NOW.isoformat())
        metric=store.get_reservation_metrics(self.account,self.pid,DAY,DAY+timedelta(days=5),now=NOW)
        self.assertEqual(metric['available_nights'],4)
        self.assertEqual(metric['booked_nights'],2)
        self.assertEqual(metric['occupancy'],.5)

    def test_missing_and_invalid_snapshots_fail_closed(self):
        self.assertFalse(store.get_reservation_metrics(self.account,self.pid,DAY,DAY+timedelta(days=5))['complete'])
        self.snapshot()
        self.assertFalse(store.get_reservation_metrics(self.account,self.pid,DAY,DAY+timedelta(days=6))['complete'])
        store.invalidate_snapshot(self.account,self.pid)
        self.assertEqual(store.get_inventory_rows(self.account,self.pid,DAY,DAY+timedelta(days=5)),[])

    def test_stale_snapshot_never_drives_financial_metrics(self):
        self.snapshot()
        later = NOW + timedelta(hours=6, seconds=1)
        metric = store.get_reservation_metrics(self.account,self.pid,DAY,DAY+timedelta(days=5),now=later)
        self.assertFalse(metric['complete'])
        self.assertIsNone(metric['adr'])

    def test_unverified_pickup_revenue_is_not_invented(self):
        self.snapshot()
        metric = store.get_reservation_metrics(self.account,self.pid,DAY,DAY+timedelta(days=5),now=NOW)
        self.assertEqual(metric['pickup_7d_nights'],2)
        self.assertIsNone(metric['pickup_7d_revenue'])

    def test_sqlite_trigger_rejects_cross_tenant_document(self):
        store.get_connection(self.account, self.pid)  # creates additive table/triggers
        with db.get_conn() as conn, self.assertRaises(sqlite3.IntegrityError):
            conn.execute('INSERT INTO operational_documents VALUES (?,?,?,?,?)',
                (self.foreign,self.pid,'connection','{}',NOW.isoformat()))

    def test_corrupted_payload_scope_fails_closed(self):
        store.save_connection(self.account,self.pid,self.mapping)
        with db.get_conn() as conn:
            payload = json.dumps({**self.mapping,'account_id':self.foreign})
            conn.execute('UPDATE operational_documents SET payload=? WHERE account_id=? AND property_id=?',
                         (payload,self.account,self.pid))
        with self.assertRaises(ValueError):
            store.get_connection(self.account,self.pid)

    def test_snapshot_rejects_unverified_accommodation_amount(self):
        calendar = [dict(propertyId=10,roomId=20,calendar=[dict(
            **{'from':(DAY+timedelta(days=i)).isoformat()},numAvail=0 if i<2 else 1,
            price1=120,minStay=1) for i in range(5)])]
        rows = normalize_snapshot(self.mapping,calendar,[self.booking()],DAY,DAY+timedelta(days=5),NOW.isoformat())
        reservations = normalize_reservations(self.mapping,[self.booking()],NOW.isoformat())
        reservations[0]['accommodation_total'] = '240'
        with self.assertRaises(ValueError):
            store.save_snapshot(self.account,self.pid,rows,reservations,DAY,DAY+timedelta(days=5),NOW.isoformat())

    def test_multiunit_booking_rejected_by_both_normalizers(self):
        b=self.booking(); b['roomQty']=2
        with self.assertRaises(Beds24Error):
            normalize_reservations(self.mapping,[b],NOW.isoformat())
        with self.assertRaises(Beds24Error):
            self.snapshot(booking=b)

    def test_source_amounts_remain_separate_from_adr_and_guest_data(self):
        b=self.booking(); b['email']='private@example.test'
        normalized=normalize_reservations(self.mapping,[b],NOW.isoformat())[0]
        self.assertEqual(normalized['source_total'],'240')
        self.assertEqual(normalized['source_tax'],'10')
        self.assertEqual(normalized['source_commission'],'30')
        self.assertIsNone(normalized['accommodation_total'])
        self.assertNotIn('email',normalized)

    def test_failed_sync_invalidates_old_snapshot(self):
        self.snapshot()
        with patch('pricepilot.services.beds24_sync.load_mapping',return_value=self.mapping), \
             patch('pricepilot.services.beds24_sync.Beds24Client') as client:
            client.return_value.calendar.side_effect=Beds24Error('synthetic transport failure')
            with self.assertRaises(Beds24Error):
                sync_property(self.account,self.pid,DAY,3)
        self.assertFalse(store.get_snapshot(self.account,self.pid)['valid'])
        client.return_value.close.assert_called_once()

    def test_incomplete_snapshot_rejected_before_replacing_valid_data(self):
        self.snapshot()
        previous=store.get_snapshot(self.account,self.pid)
        with self.assertRaises(ValueError):
            store.save_snapshot(self.account,self.pid,previous['inventory'][:-1],previous['reservations'],DAY,DAY+timedelta(days=5),NOW.isoformat())
        self.assertEqual(store.get_snapshot(self.account,self.pid),previous)

    def test_foreign_payload_rejected(self):
        with self.assertRaises(ValueError):
            store.save_snapshot(self.account,self.pid,[dict(account_id=self.foreign,property_id=self.pid)],[],DAY,DAY+timedelta(days=1),NOW.isoformat())

    def test_beds24_sync_feeds_database_occupancy_provider(self):
        start = DAY - timedelta(days=90)
        end = DAY + timedelta(days=30)
        booking = self.booking()
        calendar = [dict(propertyId=10,roomId=20,calendar=[dict(
            **{'from':(start+timedelta(days=i)).isoformat()},
            numAvail=0 if DAY <= start+timedelta(days=i) < DAY+timedelta(days=2) else 1,
            override='none',price1=120,minStay=1) for i in range((end-start).days)])]
        fake = unittest.mock.MagicMock()
        fake.calendar.return_value = calendar
        fake.bookings.return_value = [booking]
        with patch('pricepilot.services.beds24_sync.load_mapping',return_value=self.mapping), \
             patch('pricepilot.services.beds24_sync.Beds24Client',return_value=fake):
            result = sync_property(self.account,self.pid,DAY,horizon_days=1)
        observed = datetime.fromisoformat(result['observed_at'])
        occupancy = ObservedInventoryProvider(clock=lambda:observed).estimate(
            account_id=self.account,property_id=self.pid,target_date=DAY)
        self.assertEqual(occupancy.raw['booked_nights'],2)
        self.assertEqual(occupancy.raw['available_nights'],30)
        self.assertAlmostEqual(occupancy.occupancy,2/30)
        fake.close.assert_called_once()

    def test_cloud_store_is_scoped_and_confirms_write(self):
        cloud = _CloudClient(self.account, self.pid)
        with patch('pricepilot.services.operational_store.is_supabase_primary', return_value=True), \
             patch('pricepilot.services.supabase_primary._client', return_value=cloud):
            saved = store.save_connection(self.account,self.pid,self.mapping)
            self.assertEqual(store.get_connection(self.account,self.pid),saved)
            self.assertEqual(cloud.conflict,'account_id,property_id,kind')
            with self.assertRaises(ValueError):
                store.get_connection(self.foreign,self.pid)

    def test_cloud_store_never_falls_back_when_write_unconfirmed(self):
        from pricepilot.core.data_backend import CloudDatabaseUnavailable
        cloud = _CloudClient(self.account, self.pid, discard_write=True)
        with patch('pricepilot.services.operational_store.is_supabase_primary', return_value=True), \
             patch('pricepilot.services.supabase_primary._client', return_value=cloud), \
             self.assertRaises(CloudDatabaseUnavailable):
            store.save_connection(self.account,self.pid,self.mapping)
