"""Synthetic fixtures: no live Beds24 account or OTA connections used."""
from datetime import date, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import httpx

from pricepilot.integrations.beds24 import Beds24Client, Beds24Error, Beds24DeadlineExceeded
from pricepilot.services.beds24_sync import normalize_snapshot, publish_snapshot, sync_property
from pricepilot.core.plans import effective_sync_mode

DAY = date(2026, 10, 1)
MAPPING = {'account_id': 1, 'property_id': 2, 'beds24_property_id': 10, 'room_id': 20, 'price_slot': 1}
STAMP = '2026-09-10T12:00:00+00:00'


class SnapshotTests(unittest.TestCase):
    def calendar(self, availability):
        return [{'propertyId': 10, 'roomId': 20, 'calendar': [
            {'from': (DAY + timedelta(days=i)).isoformat(), 'numAvail': value,
             'override': 'none', 'price1': 100, 'minStay': 2}
            for i, value in enumerate(availability)]}]

    def booking(self, **changes):
        return {'id': 100, 'propertyId': 10, 'roomId': 20, 'arrival': DAY.isoformat(),
                'departure': (DAY + timedelta(days=2)).isoformat(), 'status': 'confirmed', **changes}

    def normalize(self, availability, bookings):
        return normalize_snapshot(MAPPING, self.calendar(availability), bookings,
                                  DAY, DAY + timedelta(days=3), STAMP)

    def test_checkout_is_open_and_block_is_not_booking(self):
        rows = self.normalize([0, 0, 1], [self.booking()])
        self.assertEqual([r['state'] for r in rows], ['booked', 'booked', 'open'])
        self.assertIsNone(rows[2]['booking_id'])
        self.assertEqual([r['state'] for r in self.normalize([0, 1, 1], [])], ['unavailable', 'open', 'open'])

    def test_cancelled_booking_does_not_count_as_occupied(self):
        rows = self.normalize([1, 1, 1], [self.booking(status='cancelled')])
        self.assertTrue(all(r['state'] == 'open' for r in rows))

    def test_beds24_blackout_is_unavailable_not_maintenance_or_booking(self):
        cal = self.calendar([0, 1, 1])
        cal[0]['calendar'][0]['override'] = 'blackout'
        rows = normalize_snapshot(MAPPING, cal, [], DAY, DAY + timedelta(days=3), STAMP)
        self.assertEqual(rows[0]['state'], 'unavailable')
        self.assertIsNone(rows[0]['booking_id'])

    def test_calendar_contradicting_booking_is_rejected(self):
        with self.assertRaisesRegex(Beds24Error, 'Conflitto'):
            self.normalize([1, 0, 1], [self.booking()])

    def test_unknown_block_or_request_requires_classification(self):
        for status in ('black', 'request', 'unexpected'):
            with self.subTest(status=status), self.assertRaises(Beds24Error):
                self.normalize([0, 0, 1], [self.booking(status=status)])

    def test_overbooking_and_duplicate_pages_are_rejected(self):
        for second in (self.booking(), self.booking(id=101)):
            with self.subTest(second=second), self.assertRaises(Beds24Error):
                self.normalize([0, 0, 1], [self.booking(), second])

    def test_missing_day_or_wrong_inventory_type_rejected(self):
        for inventory in ([1, 1], [True, 1, 1], [2, 1, 1]):
            with self.subTest(inventory=inventory), self.assertRaises(Beds24Error):
                self.normalize(inventory, [])

    def test_wrong_tenant_room_is_rejected(self):
        with self.assertRaises(Beds24Error):
            self.normalize([0, 0, 1], [self.booking(roomId=99)])

    def test_calendar_without_property_id_is_normalized(self):
        calendar = self.calendar([1, 1, 1])
        del calendar[0]['propertyId']
        rows = normalize_snapshot(MAPPING, calendar, [], DAY, DAY + timedelta(days=3), STAMP)
        self.assertEqual([row['state'] for row in rows], ['open', 'open', 'open'])

    def test_calendar_with_different_room_id_is_rejected(self):
        calendar = self.calendar([1, 1, 1])
        del calendar[0]['propertyId']
        calendar[0]['roomId'] = 99
        with self.assertRaisesRegex(Beds24Error, 'Calendario fuori dal mapping'):
            normalize_snapshot(MAPPING, calendar, [], DAY, DAY + timedelta(days=3), STAMP)

    def test_missing_booking_id_is_rejected(self):
        with self.assertRaises(Beds24Error):
            self.normalize([0, 0, 1], [self.booking(id=None)])

    def test_overlapping_calendar_ranges_rejected(self):
        cal = self.calendar([1, 1, 1])
        cal[0]['calendar'][0]['to'] = (DAY + timedelta(days=1)).isoformat()
        with self.assertRaises(Beds24Error):
            normalize_snapshot(MAPPING, cal, [], DAY, DAY + timedelta(days=3), STAMP)

    def test_unknown_calendar_restriction_is_rejected(self):
        cal = self.calendar([1, 1, 1])
        cal[0]['calendar'][0]['override'] = 'syntheticUnknown'
        with self.assertRaisesRegex(Beds24Error, 'Restrizione'):
            normalize_snapshot(MAPPING, cal, [], DAY, DAY + timedelta(days=3), STAMP)

    def test_sync_keeps_existing_decision_fields_and_manual_lock(self):
        prior = {'recommended_price': 110, 'applied_price': 105, 'decision_log_id': 7,
                 'status': 'pending_approval', 'notes': 'Existing decision'}
        rows = [{'account_id': 1, 'property_id': 2, 'date': (DAY + timedelta(days=i)).isoformat(),
                 'current_price': 101, 'observed_at': STAMP} for i in range(2)]
        with patch('pricepilot.services.beds24_sync.load_mapping', return_value=MAPPING), \
             patch('pricepilot.services.beds24_sync.Beds24Client'), \
             patch('pricepilot.services.beds24_sync.normalize_snapshot', return_value=rows), \
             patch('pricepilot.services.beds24_sync.normalize_reservations', return_value=[]), \
             patch('pricepilot.services.operational_store.invalidate_snapshot'), \
             patch('pricepilot.services.operational_store.save_snapshot'), \
             patch('pricepilot.services.beds24_sync.publish_snapshot'), \
             patch('pricepilot.core.database.get_calendar_price', side_effect=[prior, {'status': 'locked'}]), \
             patch('pricepilot.core.database.upsert_calendar_price') as write:
            sync_property(1, 2, DAY, 3)
        write.assert_called_once()
        actual = write.call_args.args[0]
        for key, value in prior.items():
            self.assertEqual(actual[key], value)
        self.assertEqual(actual['current_price'], 101)

    def test_sync_does_not_require_historical_calendar_inventory(self):
        fake = MagicMock()
        rows = [{'account_id': 1, 'property_id': 2, 'date': DAY.isoformat(),
                 'current_price': 101, 'observed_at': STAMP}]
        with patch('pricepilot.services.beds24_sync.load_mapping', return_value=MAPPING), \
             patch('pricepilot.services.beds24_sync.Beds24Client', return_value=fake), \
             patch('pricepilot.services.beds24_sync.normalize_snapshot', return_value=rows), \
             patch('pricepilot.services.beds24_sync.normalize_reservations', return_value=[]), \
             patch('pricepilot.services.operational_store.invalidate_snapshot'), \
             patch('pricepilot.services.operational_store.save_snapshot'), \
             patch('pricepilot.core.database.get_calendar_price', return_value=None), \
             patch('pricepilot.core.database.upsert_calendar_price'):
            sync_property(1, 2, DAY, 3)
        fake.calendar.assert_called_once_with(MAPPING, DAY, DAY + timedelta(days=31))
        fake.bookings.assert_called_once_with(MAPPING, DAY - timedelta(days=90), DAY + timedelta(days=32))


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'inventory.json'
        self.env = patch.dict(os.environ, {'PRICEPILOT_INVENTORY_FILE': str(self.path)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.foreign = {'account_id': 99, 'property_id': 2, 'state': 'booked'}
        self.path.write_text(json.dumps({'schema_version': 'pricepilot.inventory.v1',
                                        'observations': [self.foreign, {'account_id': 1, 'property_id': 2, 'state': 'old'}]}))

    def test_replace_only_target_property(self):
        row = {'account_id': 1, 'property_id': 2, 'state': 'open'}
        publish_snapshot([row], MAPPING)
        self.assertEqual(json.loads(self.path.read_text())['observations'], [self.foreign, row])
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_existing_lock_keeps_snapshot_intact(self):
        previous = self.path.read_bytes()
        lock = self.path.with_suffix('.json.lock')
        lock.write_text('Another process owns this lock')
        with self.assertRaises(Beds24Error):
            publish_snapshot([], MAPPING)
        self.assertTrue(lock.exists())
        self.assertEqual(self.path.read_bytes(), previous)

    def test_cross_account_payload_never_published(self):
        previous = self.path.read_bytes()
        with self.assertRaises(Beds24Error):
            publish_snapshot([self.foreign], MAPPING)
        self.assertEqual(self.path.read_bytes(), previous)

    def test_failed_atomic_replace_preserves_previous_snapshot(self):
        previous = self.path.read_bytes()
        with patch('pricepilot.services.beds24_sync.os.replace', side_effect=OSError('Synthetic failure')):
            with self.assertRaises(OSError):
                publish_snapshot([], MAPPING)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


class ClientProtocolTests(unittest.TestCase):
    def test_calendar_deadline_fails_closed_before_network(self):
        mapping = {'beds24_property_id': 357389, 'room_id': 736801, 'price_slot': 1}
        client = Beds24Client(transport=httpx.MockTransport(lambda req: self.fail('network must not be called')))
        self.addCleanup(client.close)
        with self.assertRaises(Beds24DeadlineExceeded):
            client.calendar(mapping, DAY, DAY, deadline=0)

    def test_calendar_uses_property_and_room_scope_without_record_property_id(self):
        mapping = {'beds24_property_id': 357389, 'room_id': 736801, 'price_slot': 1}

        def handle(request):
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.url.path, '/api/v2/inventory/rooms/calendar')
            self.assertEqual(request.url.params['propertyId'], '357389')
            self.assertEqual(request.url.params['roomId'], '736801')
            self.assertEqual(request.url.params['includePrices'], 'true')
            return httpx.Response(200, json={'data': [{'roomId': 736801, 'calendar': []}],
                                             'pages': {'nextPageExists': False}})

        client = Beds24Client(token='SYNTHETIC', transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        self.assertEqual(client.calendar(mapping, DAY, DAY), [{'roomId': 736801, 'calendar': []}])

    def test_calendar_rejects_wrong_room_without_synthetic_fallback(self):
        mapping = {'beds24_property_id': 357389, 'room_id': 736801, 'price_slot': 1}

        def handle(request):
            return httpx.Response(200, json={'data': [{'roomId': 736802, 'calendar': []}],
                                             'pages': {'nextPageExists': False}})

        client = Beds24Client(token='SYNTHETIC', transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        with self.assertRaisesRegex(Beds24Error, 'immobile diverso'):
            client.calendar(mapping, DAY, DAY)

    def test_invalid_calendar_response_has_no_synthetic_fallback(self):
        def handle(request):
            return httpx.Response(200, json={'data': 'not-a-list', 'pages': {'nextPageExists': False}})

        client = Beds24Client(token='SYNTHETIC', transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        with self.assertRaisesRegex(Beds24Error, 'incompleta'):
            client.calendar(MAPPING, DAY, DAY)

    def test_refresh_token_and_pagination_without_following_remote_links(self):
        seen = []
        def handle(request):
            seen.append(request)
            if request.url.path.endswith('/authentication/token'):
                self.assertEqual(request.headers['refreshToken'], 'SYNTHETIC-REFRESH')
                return httpx.Response(200, json={'token': 'SYNTHETIC-ACCESS', 'expiresIn': 86400})
            self.assertEqual(request.headers['token'], 'SYNTHETIC-ACCESS')
            self.assertEqual(request.url.host, 'beds24.com')
            page = int(request.url.params['page'])
            return httpx.Response(200, json={'data': [{'propertyId': 10, 'roomId': 20, 'id': page,
                'firstName': 'Do not retain', 'email': 'synthetic@example.test'}],
                'pages': {'nextPageExists': page == 1, 'nextPageLink': 'https://example.test/untrusted'}})
        client = Beds24Client(refresh_token='SYNTHETIC-REFRESH', transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        bookings = client.bookings(MAPPING, DAY, DAY + timedelta(days=3))
        self.assertEqual(len(seen), 3)
        self.assertEqual([b['id'] for b in bookings], [1, 2])
        self.assertTrue(all('email' not in b and 'firstName' not in b for b in bookings))

    def test_plan_never_escalates_selected_mode(self):
        for plan in ('free', 'plus', 'pro'):
            self.assertEqual(effective_sync_mode(plan, 'advisory'), 'advisory')
            self.assertEqual(effective_sync_mode(plan, None), 'advisory')
        self.assertEqual(effective_sync_mode('plus', 'auto'), 'approval')
        self.assertEqual(effective_sync_mode('pro', 'approval'), 'approval')
        self.assertEqual(effective_sync_mode('pro', 'auto'), 'approval')

    def test_pagination_flag_must_be_boolean(self):
        def handle(request):
            return httpx.Response(200, json={'data': [], 'pages': {'nextPageExists': 'false'}})
        client = Beds24Client(token='SYNTHETIC', transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        with self.assertRaisesRegex(Beds24Error, 'paginazione'):
            client.bookings(MAPPING, DAY, DAY + timedelta(days=1))
