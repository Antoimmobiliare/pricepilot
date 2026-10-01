from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from pricepilot.core.data_quality import DataUnavailable, demo_enabled
from pricepilot.pricing.safety import apply_all_safety
from pricepilot.engine.pricing_engine import calculate_recommended_price
from pricepilot.providers.observations import ObservedQuotesProvider, ObservedInventoryProvider
from pricepilot.providers.contracts import OccupancyResult
from pricepilot.integrations.beds24 import Beds24Client, Beds24Error, Beds24ChannelProvider


NOW = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
DAY = date(2026, 10, 1)


class PricingSafetyTests(unittest.TestCase):
    def test_break_even_above_ceiling_is_rejected(self):
        with self.assertRaises(ValueError):
            apply_all_safety(100, 90, 50, 120, .2, break_even=130)

    def test_floor_cannot_override_max_change(self):
        with self.assertRaises(ValueError):
            apply_all_safety(100, 150, 130, 200, .2)

    def test_all_constraints_hold_over_many_inputs(self):
        for old in (80, 100, 150):
            for requested in (1, 65, 110, 1000):
                price, _ = apply_all_safety(old, requested, 50, 200, .2, days_until=10)
                self.assertGreaterEqual(price, max(50, old * .8) - .001)
                self.assertLessEqual(price, min(200, old * 1.2) + .001)

    def test_nan_is_rejected(self):
        with self.assertRaises(ValueError):
            apply_all_safety(100, float('nan'), 50, 200, .2)

    def test_strategy_changes_the_real_engine(self):
        args = dict(base_price=150, market_avg=100, occupancy=.5, target_date=DAY,
                    min_price=20, max_price=400, days_until=20, max_change_pct=.8)
        conservative = calculate_recommended_price(**args, strategy_name='conservative')
        premium = calculate_recommended_price(**args, strategy_name='premium')
        self.assertGreater(premium['recommended_price'], conservative['recommended_price'])
        self.assertEqual(premium['breakdown']['strategy'], 'premium')

    def test_zero_occupancy_is_known_data(self):
        from pricepilot.engine.pricing_engine import _confidence_score
        self.assertEqual(_confidence_score(5,100,0), _confidence_score(5,100,.5))

    def test_production_cannot_enable_demo(self):
        for env in ('production','prod','staging','live'):
            with patch.dict(os.environ, {'PRICEPILOT_ENV':env, 'PRICEPILOT_DATA_PROVIDER':'demo'}):
                self.assertFalse(demo_enabled())


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'observations.json'

    def quote(self, nights=1, **changes):
        return {'account_id':1, 'property_id':2, 'competitor_id':'fixture-A',
                'check_in':DAY.isoformat(), 'check_out':(DAY+timedelta(days=nights)).isoformat(),
                'guests':2, 'currency':'EUR', 'room_total':str(100*nights),
                'fees_total':'30', 'taxes_total':'10', 'guest_total':str(100*nights+40),
                'room_basis':'after_discounts_excluding_fees_taxes', 'status':'available',
                'source_url':'https://example.test/fixture-A', 'collection_method':'manual_observation',
                'evidence_reference':'SYNTHETIC-TEST-FIXTURE', 'observed_at':NOW.isoformat(),
                'cancellation_policy':'standard', **changes}

    def quotes(self, rows):
        self.path.write_text(json.dumps({'schema_version':'pricepilot.quotes.v1','observations':rows}),encoding='utf-8')
        return ObservedQuotesProvider(self.path, clock=lambda:NOW)

    def test_cleaning_not_used_as_nightly_room_price(self):
        r=self.quotes([self.quote()]).analyze(property_id=2,target_date=DAY,account_id=1)
        self.assertEqual(r.market_stats['market_avg'],100)

    def test_multi_night_quote_never_becomes_single_night(self):
        p=self.quotes([self.quote(nights=3)])
        self.assertEqual(p.analyze_stay(account_id=1,property_id=2,check_in=DAY,nights=3)['quotes'][0]['average_room_per_night'],'100')
        with self.assertRaises(DataUnavailable):
            p.analyze(property_id=2,target_date=DAY,account_id=1)

    def test_scoped_data_cannot_leak(self):
        p=self.quotes([self.quote()])
        with self.assertRaises(DataUnavailable):
            p.analyze(property_id=2,target_date=DAY,account_id=9)

    def test_stale_quote_not_used(self):
        p=self.quotes([self.quote(observed_at=(NOW-timedelta(hours=7)).isoformat())])
        with self.assertRaises(DataUnavailable):
            p.analyze(property_id=2,target_date=DAY,account_id=1)

    def test_inconsistent_total_not_used(self):
        p=self.quotes([self.quote(guest_total='90')])
        with self.assertRaises(DataUnavailable):
            p.analyze(property_id=2,target_date=DAY,account_id=1)

    def test_unavailable_supersedes_previous_quote_without_becoming_booked(self):
        p=self.quotes([self.quote(observed_at=(NOW-timedelta(hours=1)).isoformat()), self.quote(status='unavailable')])
        result=p.analyze_stay(account_id=1,property_id=2,check_in=DAY)
        self.assertEqual(result['quotes'],[])
        self.assertEqual(result['unavailable'],['fixture-A'])
        self.assertNotIn('occupancy',result)

    def inventory(self, states):
        rows=[{'account_id':1,'property_id':2,'date':(DAY+timedelta(days=i)).isoformat(),
               'state':state,'observed_at':NOW.isoformat(),'source_reference':'SYNTHETIC-TEST'} for i,state in enumerate(states)]
        self.path.write_text(json.dumps({'schema_version':'pricepilot.inventory.v1','observations':rows}),encoding='utf-8')
        return ObservedInventoryProvider(self.path,clock=lambda:NOW)

    def test_occupancy_excludes_owner_and_maintenance_blocks(self):
        p=self.inventory(['open']*23+['booked']*4+['owner_blocked']*2+['maintenance_blocked'])
        result=p.estimate(property_id=2,target_date=DAY,account_id=1)
        self.assertAlmostEqual(result.occupancy,4/27)
        self.assertEqual(result.raw['target_state'],'open')

    def test_missing_inventory_is_not_open(self):
        with self.assertRaises(DataUnavailable):
            self.inventory(['open']*29).estimate(property_id=2,target_date=DAY,account_id=1)

    def test_fully_blocked_inventory_has_no_occupancy(self):
        with self.assertRaises(DataUnavailable):
            self.inventory(['owner_blocked']*30).estimate(property_id=2,target_date=DAY,account_id=1)


class Beds24Tests(unittest.TestCase):
    def setUp(self):
        self.mapping={'beds24_property_id':10,'room_id':20,'price_slot':1}
        self.calls=[]
        self.current=100

    def client(self, handler):
        c=Beds24Client(token='TEST-NOT-A-REAL-TOKEN',transport=httpx.MockTransport(handler))
        self.addCleanup(c.close)
        return c

    def response(self, **extra):
        return {'success':True,'data':[{'propertyId':10,'roomId':20,'calendar':[
            {'from':DAY.isoformat(),'to':DAY.isoformat(),'price1':self.current,'numAvail':1,'override':'none',**extra}]}]}

    def test_update_changes_only_price_and_reads_back(self):
        def handle(req):
            self.calls.append(req)
            self.assertEqual(req.url.path,'/api/v2/inventory/rooms/calendar')
            if req.method=='POST':
                body=json.loads(req.content)
                row=body[0]['calendar'][0]
                self.assertEqual(set(row),{'from','to','price1'})
                self.current=row['price1']
                return httpx.Response(201,json=[{'success':True}])
            return httpx.Response(200,json=self.response())
        result=self.client(handle).set_price(self.mapping,DAY,110)
        self.assertEqual([r.method for r in self.calls],['GET','POST','GET'])
        self.assertFalse(result['downstream_ota_verified'])

    def test_bounded_precheck_certifies_calendar_and_no_booking(self):
        self.current = 89
        def handle(req):
            self.assertEqual(req.method, 'GET')
            if req.url.path.endswith('/inventory/rooms/calendar'):
                return httpx.Response(200, json=self.response(minStay=1))
            self.assertTrue(req.url.path.endswith('/bookings'))
            return httpx.Response(200, json={'success': True, 'data': [],
                                             'pages': {'nextPageExists': False}})
        result = self.client(handle).bounded_precheck(self.mapping, DAY)
        self.assertTrue(result['certified'])
        self.assertEqual(result['price'], 89)
        self.assertEqual(result['numAvail'], 1)
        self.assertEqual(result['minStay'], 1)
        self.assertEqual(result['override'], 'none')
        self.assertFalse(result['booking_overlap'])
        self.assertEqual(result['price_slot'], 1)

    def test_bounded_precheck_fails_closed_for_missing_or_booked_data(self):
        def handle(req):
            if req.url.path.endswith('/inventory/rooms/calendar'):
                return httpx.Response(200, json=self.response(minStay=None))
            return httpx.Response(200, json={'success': True, 'data': [{
                'id': 7, 'propertyId': 10, 'roomId': 20, 'roomQty': 1,
                'status': 'confirmed', 'arrival': DAY.isoformat(),
                'departure': (DAY + timedelta(days=1)).isoformat()}],
                'pages': {'nextPageExists': False}})
        result = self.client(handle).bounded_precheck(self.mapping, DAY)
        self.assertFalse(result['certified'])
        self.assertTrue(result['booking_overlap'])
        self.assertIsNone(result['minStay'])

    def test_bounded_precheck_rejects_unstructured_provider_records(self):
        def handle(req):
            if req.url.path.endswith('/inventory/rooms/calendar'):
                return httpx.Response(200, json={'success': True, 'data': [None],
                                                 'pages': {'nextPageExists': False}})
            return httpx.Response(200, json={'success': True, 'data': [],
                                             'pages': {'nextPageExists': False}})
        with self.assertRaisesRegex(Beds24Error, 'non compatibile'):
            self.client(handle).bounded_precheck(self.mapping, DAY)

    def test_blocked_day_never_writes(self):
        def handle(req):
            self.assertEqual(req.method,'GET')
            return httpx.Response(200,json=self.response(numAvail=0,override='blackout'))
        with self.assertRaises(Beds24Error): self.client(handle).set_price(self.mapping,DAY,110)

    def test_wrong_room_response_is_rejected(self):
        def handle(req):
            payload=self.response(); payload['data'][0]['roomId']=99
            return httpx.Response(200,json=payload)
        with self.assertRaises(Beds24Error): self.client(handle).current_day(self.mapping,DAY)

    def test_readback_mismatch_is_not_success(self):
        def handle(req):
            return httpx.Response(201,json=[{'success':True}]) if req.method=='POST' else httpx.Response(200,json=self.response())
        with self.assertRaises(Beds24Error): self.client(handle).set_price(self.mapping,DAY,110)

    def test_rate_limit_is_explicit(self):
        with self.assertRaisesRegex(Beds24Error,'Limite API'):
            self.client(lambda req:httpx.Response(429)).current_day(self.mapping,DAY)

    def test_remote_errors_do_not_expose_response_secrets(self):
        with self.assertRaises(Beds24Error) as ctx:
            self.client(lambda req:httpx.Response(401,text='private-token-sensitive-body')).current_day(self.mapping,DAY)
        self.assertNotIn('private-token',str(ctx.exception))

    def test_no_credentials_no_network(self):
        c=Beds24Client(transport=httpx.MockTransport(lambda req:self.fail('Unexpected network')))
        self.addCleanup(c.close)
        with self.assertRaises(Beds24Error): c.current_day(self.mapping,DAY)

    def test_default_write_gate(self):
        with patch.dict(os.environ,{},clear=True):
            result=Beds24ChannelProvider().update_price(prop={'id':1,'account_id':1},new_price=100,target_date=DAY)
        self.assertFalse(result.ok)


class SchedulerHorizonTests(unittest.TestCase):
    def run_cycle(self, state='open', fail=False):
        from contextlib import ExitStack
        from pricepilot.core.scheduler import run_pricing_cycle
        from pricepilot.providers import registry
        from pricepilot.core import database
        self.days=[]
        provider=Mock(name='fixture_inventory')
        provider.name='test_inventory'
        provider.estimate.return_value=OccupancyResult(.5,'test',{'target_state':state})
        def decision(**kw):
            self.days.append(kw['target_date'])
            if fail: raise DataUnavailable('No observation')
            return {'mode':'advisory'}
        with ExitStack() as stack:
            for name,val in {'get_account':{'plan':'free','billing_status':'dev'}, 'get_properties':[{'id':1,'account_id':1}], 'try_start_operation_run':(1,None),
                             'finish_operation_run':{'id':1},'record_audit_event':None}.items():
                stack.enter_context(patch.object(database,name,return_value=val))
            stack.enter_context(patch.object(registry,'get_occupancy_provider',return_value=provider))
            stack.enter_context(patch('pricepilot.engine.decision_engine.process_decision',side_effect=decision))
            return run_pricing_cycle(target_date=DAY,horizon_days=3)

    def test_every_future_day_is_analyzed(self):
        self.run_cycle()
        self.assertEqual(self.days,[DAY,DAY+timedelta(days=1),DAY+timedelta(days=2)])

    def test_booked_dates_are_skipped(self):
        self.run_cycle('booked')
        self.assertEqual(self.days,[])

    def test_partial_errors_carry_target_date(self):
        result=self.run_cycle(fail=True)
        self.assertEqual(len(result['errors']),3)
        self.assertEqual(result['errors'][1]['date'],(DAY+timedelta(days=1)).isoformat())

    def test_cycle_deadline_finishes_run_when_lock_path_stalls(self):
        from pricepilot.core import database, scheduler
        from pricepilot.core.scheduler import run_pricing_cycle
        provider = Mock(name='fixture_inventory')
        provider.name = 'test_inventory'
        provider.estimate.return_value = OccupancyResult(.5, 'test', {'target_state': 'open'})
        finished = Mock(return_value={'id': 1, 'status': 'error'})
        with patch.object(database, 'get_account', return_value={'plan': 'free', 'billing_status': 'dev'}), \
             patch.object(database, 'get_properties', return_value=[{'id': 1, 'account_id': 1}]), \
             patch.object(database, 'try_start_operation_run', return_value=(1, None)), \
             patch.object(database, 'finish_operation_run', finished), \
             patch.object(database, 'record_audit_event'), \
             patch.object(scheduler, 'time') as clock, \
             patch('pricepilot.providers.registry.get_occupancy_provider', return_value=provider), \
             patch('pricepilot.engine.decision_engine.process_decision'):
            clock.monotonic.side_effect = [0.0, 2.0]
            with patch.dict(os.environ, {'PRICEPILOT_CYCLE_TIMEOUT_SECONDS': '1'}):
                with self.assertRaises(TimeoutError):
                    run_pricing_cycle(account_id=1, target_date=DAY, horizon_days=3)
        finished.assert_called_once()
        self.assertEqual(finished.call_args.kwargs['status'], 'error')

    def test_cloud_reports_child_failure(self):
        from pricepilot.core.scheduler import run_cloud_pricing_cycle
        with patch('pricepilot.core.database.get_properties',return_value=[{'account_id':1}]), patch('pricepilot.core.scheduler.run_pricing_cycle',return_value={'errors':[{'error':'missing'}]}):
            result=run_cloud_pricing_cycle(target_date=DAY)
        self.assertFalse(result['ok'])
        self.assertEqual(result['accounts_failed'],1)


class ApiBindingTests(unittest.TestCase):
    def test_get_pricing_never_creates_decisions(self):
        from fastapi.testclient import TestClient
        from pricepilot.api import server
        with patch.dict(os.environ,{'PRICEPILOT_ENV':'development'},clear=True), patch.object(server,'process_decision') as engine:
            response=TestClient(server.app).get('/pricing')
        self.assertEqual(response.status_code,405)
        engine.assert_not_called()

    def test_telegram_http_request_binds_body(self):
        from fastapi.testclient import TestClient
        from pricepilot.api import server
        with patch.dict(os.environ,{'PRICEPILOT_ENV':'development'},clear=True), patch.object(server,'tg_process_webhook') as handler:
            client=TestClient(server.app)
            response=client.post('/telegram/webhook',json={'update_id':123})
            self.assertEqual(response.status_code,200)
            handler.assert_called_once_with({'update_id':123})


class ApprovalIntegrityTests(unittest.TestCase):
    def setUp(self):
        from pricepilot.core import database as db
        from pricepilot.core.config import CONFIG
        self.db=db
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old=CONFIG['db_path']
        CONFIG['db_path']=str(Path(self.tmp.name)/'approval.db')
        self.addCleanup(lambda: CONFIG.update(db_path=self.old))
        db.init_db()
        self.account=db.create_account('Approval test',plan='plus',billing_status='active')
        from pricepilot.services.property_service import create_property
        self.prop=create_property({'account_id':self.account['id'],'name':'Fixture','min_price':50,'max_price':200,'plan':'plus','sync_mode':'approval','platform':'airbnb'})
        self.log=db.save_decision_log({'account_id':self.account['id'],'property_id':self.prop['id'],
            'old_price':100,'new_price':110,'decision':'PENDING_APPROVAL','mode':'approval','applied':0,'date':DAY.isoformat()})
        self.calendar={'account_id':self.account['id'],'property_id':self.prop['id'],'date':DAY.isoformat(),
                       'current_price':100,'recommended_price':110,'current_price_source':'manual','decision_log_id':self.log,'status':'pending_approval'}
        db.upsert_calendar_price(self.calendar)

    def test_repeated_approval_sends_once(self):
        from pricepilot.engine.decision_engine import approve_decision
        # This test isolates idempotency from production inventory freshness.
        with patch('pricepilot.engine.decision_engine.demo_enabled', return_value=True), \
             patch('pricepilot.engine.decision_engine._channel_manager_update',return_value={'ok':True,'is_real':True,'platform':'test','listing_id':'test'}) as send:
            self.assertTrue(approve_decision(self.log,self.account['id'])['applied'])
            self.assertEqual(approve_decision(self.log,self.account['id'])['status'],'already_applied')
        self.assertEqual(send.call_count,1)

    def test_new_manual_lock_blocks_approval(self):
        from pricepilot.engine.decision_engine import approve_decision
        self.db.upsert_calendar_price({**self.calendar,'status':'locked'})
        with patch('pricepilot.engine.decision_engine._channel_manager_update') as send:
            self.assertEqual(approve_decision(self.log,self.account['id'])['status'],'stale')
        send.assert_not_called()

    def test_changed_current_price_blocks_approval(self):
        from pricepilot.engine.decision_engine import approve_decision
        self.db.upsert_calendar_price({**self.calendar,'current_price':105})
        with patch('pricepilot.engine.decision_engine._channel_manager_update') as send:
            self.assertEqual(approve_decision(self.log,self.account['id'])['status'],'stale')
        send.assert_not_called()

    def test_only_one_database_claim_wins(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.db.claim_decision_application(self.log,self.account['id'],'PENDING_APPROVAL'),range(2)))
        self.assertEqual(sum(results),1)

    def test_rejected_decision_never_sends(self):
        from pricepilot.engine.decision_engine import approve_decision
        self.db.mark_decision_rejected(self.log,self.account['id'])
        with patch('pricepilot.engine.decision_engine._channel_manager_update') as send:
            self.assertEqual(approve_decision(self.log,self.account['id'])['status'],'not_pending')
        send.assert_not_called()
