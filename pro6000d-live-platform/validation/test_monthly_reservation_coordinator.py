"""Exercise controller reservation recovery with an in-memory durable store."""
import pathlib
import sqlite3
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'backend'))
from monthly_reservation import ReservationCoordinator, ReservationUncertain, ReservationDeferred
from controller_bridge import ControllerBridge, ControllerDenied, ControllerOutcomeUnknown, ControllerUnavailable

class Controller:
    def __init__(self): self.holds = {}; self.calls = []; self.lose_reserve = False; self.lose_bind = False; self.deny_once = False; self.lose_roll = False; self.deny_roll = False; self.month = 202611
    def reservation_status(self, uid, request):
        return self.holds.get(request, {'uid':uid,'request_id':request,'present':False})
    def reserve(self, uid, request, tier, gpu, minutes):
        self.calls.append(('reserve',request))
        existing=self.holds.get(request)
        if existing:
            if existing['state']=='cancelled' or (existing['uid'],existing['tier'],existing['gpu'],existing['minutes']) != (uid,tier,gpu,minutes):
                raise ControllerUnavailable('terminal or conflicting reservation')
            return {'request_id':request}
        if self.deny_once:
            self.deny_once = False
            raise ControllerDenied('capacity')
        self.holds[request] = {'present':True,'uid':uid,'request_id':request,'month':self.month,'tier':tier,
                               'gpu':gpu,'minutes':minutes,'state':'unbound','scheduler_id':None}
        if self.lose_reserve: self.lose_reserve = False; raise ReservationUncertain('lost')
        return {'request_id':request}
    def rollover_reservation(self, uid, request):
        self.calls.append(('roll',request))
        if self.deny_roll: raise ControllerDenied('capacity')
        self.holds[request]['month'] = self.month
        if self.lose_roll:
            self.lose_roll = False
            raise ControllerOutcomeUnknown('lost reply')
        return {'rolled':True}
    def cancel_request(self, uid, request, tier, gpu, minutes):
        self.calls.append(('cancel_request',request))
        state=self.holds.get(request)
        if state and (state['uid'],state['tier'],state['gpu'],state['minutes']) != (uid,tier,gpu,minutes):
            raise ControllerOutcomeUnknown('conflicting identity')
        if state and state['state'] not in ('unbound','cancelled'):
            raise ControllerOutcomeUnknown('not unbound')
        if not state:
            state={'present':True,'uid':uid,'request_id':request,'month':self.month,
                   'tier':tier,'gpu':gpu,'minutes':minutes,'scheduler_id':None}
            self.holds[request]=state
        state['state']='cancelled'
        return {'cancelled':True}
    def cancel_unbound_reservation(self, uid, request):
        self.calls.append(('cancel',request))
        self.holds[request]['state'] = 'cancelled'
        return {'cancelled':True}
    def cancel_bound_reservation(self, uid, request, sid):
        self.calls.append(('cancel_bound',request,sid))
        self.holds[request].update(state='cancelled_bound',scheduler_id=sid)
        return {'cancelled':True}
    def bind_reservation(self, uid, request, sid):
        self.calls.append(('bind',request,sid))
        self.holds[request].update(state='bound', scheduler_id=sid)
        if self.lose_bind: self.lose_bind = False; raise ReservationUncertain('lost')
        return {'bound':True}

class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.bridge = Controller()
        self.coordinator = ReservationCoordinator(self.db, self.bridge)
        self.ident = 'a'*32
    def tearDown(self): self.db.close()
    def test_lost_reserve_reply_recovers_without_second_reservation(self):
        self.bridge.lose_reserve = True
        with self.assertRaises(ReservationUncertain): self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.assertEqual(self.bridge.calls, [('reserve',self.ident)])
    def test_crash_before_reserve_send_retries_same_identity(self):
        self.db.execute("INSERT INTO monthly_dispatch_holds VALUES (?,?,?,?,?,?,?,?)", (self.ident,1001,'debug',1,60,202611,'REQUESTING',None))
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.assertEqual(self.bridge.calls, [('reserve',self.ident)])
        self.assertEqual(self.db.execute('SELECT state FROM monthly_dispatch_holds').fetchone()[0], 'HELD')
    def test_conflicting_persisted_or_remote_identity_fails_closed(self):
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        with self.assertRaises(ReservationUncertain): self.coordinator.hold(self.ident,1001,'normal',1,60,202611)
        self.bridge.holds[self.ident]['gpu'] = 2
        with self.assertRaises(ReservationUncertain): self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
    def test_lost_bind_reply_is_reconciled_without_second_bind(self):
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.bridge.lose_bind = True
        with self.assertRaises(ReservationUncertain): self.coordinator.bind(self.ident,1001,42)
        self.coordinator.bind(self.ident,1001,42)
        self.assertEqual([c[0] for c in self.bridge.calls], ['reserve','bind'])
    def test_released_allocation_requires_final_reconciliation(self):
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.coordinator.bind(self.ident,1001,42)
        self.bridge.holds[self.ident].update(state='released', generation=7, sluid=19)
        with self.assertRaises(ReservationUncertain): self.coordinator.bind(self.ident,1001,42)
        self.coordinator.bind(self.ident,1001,42, allow_released=True)
        with self.assertRaises(ReservationUncertain): self.coordinator.bind(self.ident,1001,43, allow_released=True)
    def test_definite_denial_can_retry_after_month_rollover(self):
        self.bridge.deny_once = True
        with self.assertRaises(ReservationDeferred): self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.bridge.month = 202612
        self.coordinator.hold(self.ident,1001,'debug',1,60,202612)
        self.assertEqual(self.db.execute('SELECT month FROM monthly_dispatch_holds').fetchone()[0], 202612)
    def test_unbound_hold_rolls_to_new_month_without_new_identity(self):
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.bridge.month = 202612
        self.coordinator.hold(self.ident,1001,'debug',1,60,202612)
        self.assertEqual(self.db.execute('SELECT month FROM monthly_dispatch_holds').fetchone()[0], 202612)
        self.assertEqual(self.bridge.calls, [('reserve',self.ident),('roll',self.ident)])
    def test_rollover_response_lost_recovers_controller_month(self):
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.bridge.month = 202612
        self.bridge.holds[self.ident]['month'] = 202612
        self.coordinator.hold(self.ident,1001,'debug',1,60,202612)
        self.assertEqual(self.db.execute('SELECT month FROM monthly_dispatch_holds').fetchone()[0], 202612)
        self.assertEqual(self.bridge.calls, [('reserve',self.ident)])
    def test_lost_rollover_reply_while_binding_unbound_recovers(self):
        import monthly_reservation
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.db.execute("UPDATE monthly_dispatch_holds SET state='BINDING',scheduler_id=42 WHERE job_id=?",(self.ident,))
        self.bridge.holds[self.ident]['month'] = 202612
        old=monthly_reservation.month_now
        monthly_reservation.month_now=lambda:'2026-12'
        try: self.coordinator.bind(self.ident,1001,42)
        finally: monthly_reservation.month_now=old
        row=self.db.execute('SELECT month,state FROM monthly_dispatch_holds').fetchone()
        self.assertEqual(tuple(row),(202612,'BOUND'))
        self.assertEqual(self.bridge.holds[self.ident]['scheduler_id'],42)
    def test_bound_queued_request_rolls_before_retry(self):
        import monthly_reservation
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.coordinator.bind(self.ident,1001,42)
        self.bridge.month=202612
        old=monthly_reservation.month_now
        monthly_reservation.month_now=lambda:'2026-12'
        try: self.coordinator.bind(self.ident,1001,42)
        finally: monthly_reservation.month_now=old
        self.assertEqual(self.db.execute('SELECT month FROM monthly_dispatch_holds').fetchone()[0],202612)
        self.assertEqual(self.bridge.holds[self.ident]['state'],'bound')
    def test_rollover_denial_preserves_old_hold_and_can_retry(self):
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.bridge.month=202612; self.bridge.deny_roll=True
        with self.assertRaises(ReservationDeferred): self.coordinator.hold(self.ident,1001,'debug',1,60,202612)
        self.assertEqual(self.bridge.holds[self.ident]['month'],202611)
        self.assertEqual(self.db.execute('SELECT month FROM monthly_dispatch_holds').fetchone()[0],202611)
        self.bridge.deny_roll=False
        self.coordinator.hold(self.ident,1001,'debug',1,60,202612)
        self.assertEqual(self.bridge.holds[self.ident]['month'],202612)
    def test_actual_lost_rollover_reply_reconciles_without_second_roll(self):
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.bridge.month=202612; self.bridge.lose_roll=True
        with self.assertRaises(ReservationUncertain): self.coordinator.hold(self.ident,1001,'debug',1,60,202612)
        self.coordinator.hold(self.ident,1001,'debug',1,60,202612)
        self.assertEqual([call[0] for call in self.bridge.calls],['reserve','roll'])
    def test_bridge_parses_cancelled_bound_identity(self):
        bridge=ControllerBridge('/unused',0)
        bridge._exchange=lambda *args,**kwargs:'OK 2 FOUND 202611 0 1 60 3600000000000 42 0 6 0'
        result=bridge.reservation_status(1001,self.ident)
        self.assertEqual((result['state'],result['scheduler_id'],result['hold_active']),('cancelled_bound',42,False))
    def test_bridge_accepts_only_explicit_capacity_denial(self):
        bridge = ControllerBridge('/unused',0)
        bridge._exchange = lambda *args, **kwargs: 'DENIED capacity'
        with self.assertRaises(ControllerDenied): bridge.reserve(1001,self.ident,'debug',1,60)
    def test_competing_denial_cannot_turn_unconfirmed_hold_into_submission(self):
        original=self.bridge.reserve
        def reserve(*args):
            result=original(*args)
            self.db.execute("UPDATE monthly_dispatch_holds SET state='DEFERRED' WHERE job_id=?", (self.ident,))
            return result
        self.bridge.reserve=reserve
        with self.assertRaises(ReservationUncertain):
            self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.assertEqual(self.db.execute('SELECT state FROM monthly_dispatch_holds').fetchone()[0],'DEFERRED')
        self.bridge.reserve=original
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.assertEqual(self.db.execute('SELECT state FROM monthly_dispatch_holds').fetchone()[0],'HELD')
    def test_cancelled_job_cannot_create_new_hold(self):
        self.db.execute('CREATE TABLE jobs (id TEXT PRIMARY KEY,state TEXT,owner_uid INTEGER,selected_tier TEXT)')
        self.db.execute('INSERT INTO jobs VALUES(?,?,?,?)',(self.ident,'CANCELLING_RESERVATION',1001,'debug'))
        with self.assertRaises(ReservationUncertain):
            self.coordinator.hold(self.ident,1001,'debug',1,60,202611,require_waiting=True)
        self.assertIsNone(self.db.execute('SELECT * FROM monthly_dispatch_holds').fetchone())
        self.assertEqual(self.bridge.calls, [])
    def test_definite_capacity_denial_can_retry_same_identity(self):
        self.bridge.deny_once = True
        with self.assertRaises(ReservationDeferred): self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.assertEqual(self.db.execute('SELECT state FROM monthly_dispatch_holds').fetchone()[0], 'DEFERRED')
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.assertEqual(self.bridge.calls, [('reserve',self.ident),('reserve',self.ident)])
    def test_cancel_confirmed_unbound_hold(self):
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.coordinator.cancel(self.ident,1001)
        self.assertEqual(self.bridge.holds[self.ident]['state'], 'cancelled')
        self.assertEqual(self.db.execute('SELECT state FROM monthly_dispatch_holds').fetchone()[0], 'CANCELLED')
    def test_cancel_before_reserve_send_creates_controller_tombstone(self):
        self.db.execute("INSERT INTO monthly_dispatch_holds VALUES (?,?,?,?,?,?,?,?)", (self.ident,1001,'debug',1,60,202611,'REQUESTING',None))
        self.coordinator.cancel(self.ident,1001)
        self.assertEqual(self.bridge.calls, [('cancel_request',self.ident)])
        self.assertEqual(self.bridge.holds[self.ident]['state'],'cancelled')
        self.assertEqual(self.db.execute('SELECT state FROM monthly_dispatch_holds').fetchone()[0], 'CANCELLED')
        with self.assertRaises(ControllerUnavailable): self.bridge.reserve(1001,self.ident,'debug',1,60)
        self.assertEqual(self.bridge.holds[self.ident]['state'],'cancelled')
    def test_lost_cancel_reply_retries_same_tombstone(self):
        self.db.execute("INSERT INTO monthly_dispatch_holds VALUES (?,?,?,?,?,?,?,?)", (self.ident,1001,'debug',1,60,202611,'REQUESTING',None))
        original=self.bridge.cancel_request
        def cancel_once(*args):
            original(*args)
            self.bridge.cancel_request=original
            raise ControllerOutcomeUnknown('lost reply')
        self.bridge.cancel_request=cancel_once
        with self.assertRaises(ReservationUncertain): self.coordinator.cancel(self.ident,1001)
        self.coordinator.cancel(self.ident,1001)
        self.assertEqual(self.bridge.holds[self.ident]['state'],'cancelled')
        self.assertEqual(self.db.execute('SELECT state FROM monthly_dispatch_holds').fetchone()[0], 'CANCELLED')
    def test_cancel_never_refunds_bound_hold(self):
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.coordinator.bind(self.ident,1001,42)
        with self.assertRaises(ReservationUncertain): self.coordinator.cancel(self.ident,1001)
        self.assertNotIn(('cancel',self.ident), self.bridge.calls)
    def test_final_unstarted_bound_job_releases_hold(self):
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.coordinator.bind(self.ident,1001,42)
        self.coordinator.finish(self.ident,1001,42)
        self.assertEqual(self.bridge.holds[self.ident]['state'],'cancelled_bound')
        self.assertEqual(self.db.execute('SELECT state FROM monthly_dispatch_holds').fetchone()[0],'CLOSED')
        self.coordinator.finish(self.ident,1001,42)
    def test_final_unbound_job_recovers_after_lost_submission_reply(self):
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.coordinator.finish(self.ident,1001,42)
        row=self.db.execute('SELECT state,scheduler_id FROM monthly_dispatch_holds').fetchone()
        self.assertEqual(tuple(row),('CLOSED',42))
        self.assertEqual(self.bridge.holds[self.ident]['state'],'cancelled_bound')
    def test_terminal_rollover_reply_survives_multiple_months_offline(self):
        import monthly_reservation
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.coordinator.bind(self.ident,1001,42)
        self.bridge.holds[self.ident].update(state='cancelled_bound',month=202612)
        old=monthly_reservation.month_now;monthly_reservation.month_now=lambda:'2027-01'
        try:self.coordinator.finish(self.ident,1001,42)
        finally:monthly_reservation.month_now=old
        row=self.db.execute('SELECT state,month FROM monthly_dispatch_holds').fetchone()
        self.assertEqual(tuple(row),('CLOSED',202612))
    def test_final_active_job_is_not_treated_as_unstarted(self):
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.coordinator.bind(self.ident,1001,42)
        self.bridge.holds[self.ident].update(state='active',generation=3,sluid=11)
        with self.assertRaises(ReservationUncertain): self.coordinator.finish(self.ident,1001,42)
        self.assertNotIn(('cancel_bound',self.ident,42),self.bridge.calls)
    def test_final_released_running_job_is_idempotent(self):
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.coordinator.bind(self.ident,1001,42)
        self.bridge.holds[self.ident].update(state='released',generation=3,sluid=11)
        self.coordinator.finish(self.ident,1001,42)
        self.assertEqual(self.db.execute('SELECT state FROM monthly_dispatch_holds').fetchone()[0],'CLOSED')
    def test_other_job_cannot_steal_binding(self):
        self.coordinator.hold(self.ident,1001,'debug',1,60,202611)
        self.coordinator.bind(self.ident,1001,42)
        with self.assertRaises(ReservationUncertain): self.coordinator.bind(self.ident,1001,43)

if __name__ == '__main__': unittest.main()
