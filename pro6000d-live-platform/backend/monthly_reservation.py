"""Durable broker-side coordination of controller-owned GPU-hour holds.

The controller remains the allocation authority. An ambiguous response never
causes a fresh reservation or submission under a different identity.
"""
import re
from monthly_quota import month_now
from controller_bridge import ControllerDenied, ControllerUnavailable


class ReservationUncertain(Exception):
    pass


class ReservationDeferred(ReservationUncertain):
    """The controller confirmed insufficient capacity and no hold was created."""


class ReservationCoordinator:
    def __init__(self, db, bridge):
        self.db, self.bridge = db, bridge
        db.execute('''CREATE TABLE IF NOT EXISTS monthly_dispatch_holds (
            job_id TEXT PRIMARY KEY, uid INTEGER NOT NULL, tier TEXT NOT NULL,
            gpu INTEGER NOT NULL, minutes INTEGER NOT NULL, month INTEGER NOT NULL,
            state TEXT NOT NULL, scheduler_id INTEGER)''')

    def _row(self, job_id):
        return self.db.execute('SELECT * FROM monthly_dispatch_holds WHERE job_id=?', (job_id,)).fetchone()

    @staticmethod
    def _validate(job_id, uid, tier, gpu, minutes, month):
        if (not isinstance(job_id, str) or not re.fullmatch(r'[0-9a-f]{32}', job_id)
                or isinstance(uid, bool) or not isinstance(uid, int) or not 0 < uid <= 2**32-1
                or tier not in ('debug', 'important', 'normal')
                or isinstance(gpu, bool) or not isinstance(gpu, int) or not 0 < gpu <= 2**32-1
                or isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 < minutes <= 2**32-1
                or isinstance(month, bool) or not isinstance(month, int) or not 197001 <= month <= 999912
                or not 1 <= month % 100 <= 12 or gpu * minutes * 60 * 10**9 > 2**64-1):
            raise ReservationUncertain('invalid reservation request')

    def _status(self, row, rollover_month=None):
        try:
            status = self.bridge.reservation_status(row['uid'], row['job_id'])
        except ControllerUnavailable as exc:
            raise ReservationUncertain('reservation status unavailable') from exc
        if not status.get('present'):
            raise ReservationUncertain('reservation absent; outcome is unknown')
        if any(status.get(k) != row[k] for k in ('uid', 'tier', 'gpu', 'minutes')):
            raise ReservationUncertain('reservation fields differ from durable intent')
        if status.get('month') != row['month']:
            # Adopt a lost rollover reply only for the same unstarted request
            # and scheduler binding. Never infer a fresh hold from absence.
            remote_month = status.get('month')
            if (rollover_month is None or isinstance(remote_month, bool) or not isinstance(remote_month, int) or
                    not 197001 <= remote_month <= 999912 or not 1 <= remote_month % 100 <= 12 or
                    not row['month'] < remote_month <= rollover_month or
                    (status.get('scheduler_id') != row['scheduler_id'] and not
                     (row['state'] == 'BINDING' and status.get('state') == 'unbound' and
                      status.get('scheduler_id') is None and row['scheduler_id'] is not None)) or
                    status.get('state') not in ('unbound', 'bound', 'prepared', 'active', 'released', 'cancelled_bound')):
                raise ReservationUncertain('reservation month differs from durable intent')
            self.db.execute('UPDATE monthly_dispatch_holds SET month=? WHERE job_id=? AND month=?',
                            (remote_month, row['job_id'], row['month']))
            if self._row(row['job_id'])['month'] != remote_month:
                raise ReservationUncertain('reservation month changed concurrently')
        return status

    def _rollover(self, row, status, month):
        if status['month'] == month:
            return status
        if status['month'] > month or status.get('state') not in ('unbound', 'bound'):
            raise ReservationUncertain('reservation cannot roll into this month')
        try:
            self.bridge.rollover_reservation(row['uid'], row['job_id'])
        except ControllerDenied as exc:
            raise ReservationDeferred('new month cannot cover reservation') from exc
        except ControllerUnavailable as exc:
            raise ReservationUncertain('reservation rollover outcome unknown') from exc
        status = self._status(self._row(row['job_id']), rollover_month=month)
        if status['month'] != month:
            raise ReservationUncertain('controller did not confirm new reservation month')
        return status

    def hold(self, job_id, uid, tier, gpu, minutes, month, require_waiting=False):
        self._validate(job_id, uid, tier, gpu, minutes, month)
        # Persist the immutable request first. The controller treats a repeat
        # of the same request as idempotent and retains cancellation tombstones.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if require_waiting:
                job = self.db.execute('SELECT state,owner_uid,selected_tier FROM jobs WHERE id=?', (job_id,)).fetchone()
                if not job or job['state'] != 'WAITING_QUOTA' or job['owner_uid'] != uid or job['selected_tier'] != tier:
                    raise ReservationUncertain('job no longer waiting for this reservation')
            row = self._row(job_id)
            should_send = False
            if row is None:
                self.db.execute('INSERT INTO monthly_dispatch_holds VALUES(?,?,?,?,?,?,?,?)',
                                (job_id, uid, tier, gpu, minutes, month, 'REQUESTING', None))
                should_send = True
            else:
                if tuple(row[k] for k in ('uid', 'tier', 'gpu', 'minutes')) != (uid, tier, gpu, minutes):
                    raise ReservationUncertain('reservation intent conflicts')
                if row['state'] == 'DEFERRED' and row['scheduler_id'] is None:
                    # A definite denial wrote no hold. A new month may safely
                    # use the same identity after updating the durable intent.
                    self.db.execute("UPDATE monthly_dispatch_holds SET state='REQUESTING',month=? WHERE job_id=? AND state='DEFERRED'", (month, job_id))
                    should_send = True
                elif row['state'] not in ('REQUESTING', 'HELD') or row['scheduler_id'] is not None or row['month'] > month:
                    raise ReservationUncertain('reservation has already advanced or month changed')
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise
        row = self._row(job_id)
        if not should_send and row['state'] == 'REQUESTING':
            try:
                observed = self.bridge.reservation_status(uid, job_id)
            except ControllerUnavailable as exc:
                raise ReservationUncertain('reservation status unavailable') from exc
            should_send = not observed.get('present')
        if should_send:
            try:
                self.bridge.reserve(uid, job_id, tier, gpu, minutes)
            except ControllerDenied as exc:
                self.db.execute("UPDATE monthly_dispatch_holds SET state='DEFERRED' WHERE job_id=? AND state='REQUESTING'", (job_id,))
                raise ReservationDeferred('selected tier cannot cover the requested GPU time') from exc
            except ControllerUnavailable as exc:
                raise ReservationUncertain('reservation outcome unknown') from exc
        status = self._status(row, rollover_month=month)
        if status.get('state') != 'unbound' or status.get('scheduler_id') is not None:
            raise ReservationUncertain('reservation is not unbound')
        if status['month'] != month:
            status = self._rollover(self._row(job_id), status, month)
        self.db.execute("UPDATE monthly_dispatch_holds SET state='HELD' WHERE job_id=? AND state='REQUESTING' AND scheduler_id IS NULL", (job_id,))
        confirmed = self._row(job_id)
        if (not confirmed or confirmed['state'] != 'HELD' or confirmed['scheduler_id'] is not None or
                tuple(confirmed[k] for k in ('uid','tier','gpu','minutes','month')) != (uid,tier,gpu,minutes,month)):
            raise ReservationUncertain('reservation intent changed before hold confirmation')

    def cancel(self, job_id, uid):
        """Cancel only after the caller fenced scheduler submission in SQLite."""
        row = self._row(job_id)
        if row is None:
            return  # No intent can be created after the caller's fence.
        if row['uid'] != uid or row['scheduler_id'] is not None or row['state'] not in ('REQUESTING', 'HELD', 'DEFERRED', 'CANCELLED'):
            raise ReservationUncertain('reservation cannot be safely cancelled')
        # One controller transaction either cancels an existing unbound hold
        # or creates an absent-request tombstone. This fences in-flight RESERVE1.
        try:
            self.bridge.cancel_request(uid, job_id, row['tier'], row['gpu'], row['minutes'])
        except (ControllerUnavailable, ValueError) as exc:
            raise ReservationUncertain('cancellation outcome unknown') from exc
        self.db.execute("UPDATE monthly_dispatch_holds SET state='CANCELLED' WHERE job_id=? AND state IN ('REQUESTING','HELD','DEFERRED','CANCELLED') AND scheduler_id IS NULL", (job_id,))
        if self._row(job_id)['state'] != 'CANCELLED':
            raise ReservationUncertain('reservation changed while cancelling')

    def finish(self, job_id, uid, scheduler_id):
        """Reconcile a terminal scheduler job without refunding live allocation."""
        if isinstance(scheduler_id, bool) or not isinstance(scheduler_id, int) or not 0 < scheduler_id <= 2**32-1:
            raise ReservationUncertain('invalid scheduler identity')
        row = self._row(job_id)
        if not row or row['uid'] != uid or row['scheduler_id'] not in (None, scheduler_id):
            raise ReservationUncertain('terminal reservation identity mismatch')
        if row['state'] == 'HELD' and row['scheduler_id'] is None:
            self.db.execute("UPDATE monthly_dispatch_holds SET state='BINDING',scheduler_id=? WHERE job_id=? AND state='HELD' AND scheduler_id IS NULL", (scheduler_id,job_id))
            row = self._row(job_id)
        if row['scheduler_id'] != scheduler_id:
            raise ReservationUncertain('terminal scheduler identity unavailable')
        if row['state'] == 'CLOSED':
            return
        if row['state'] not in ('BINDING', 'BOUND'):
            raise ReservationUncertain('terminal reservation state unavailable')
        status = self._status(row, rollover_month=int(month_now().replace('-', '')))
        state, bound = status.get('state'), status.get('scheduler_id')
        if state in ('unbound', 'bound') and bound in (None, scheduler_id):
            try:
                self.bridge.cancel_bound_reservation(uid, job_id, scheduler_id)
            except ControllerUnavailable as exc:
                raise ReservationUncertain('terminal reservation cancellation unconfirmed') from exc
            status = self._status(self._row(job_id), rollover_month=int(month_now().replace('-', '')))
            state, bound = status.get('state'), status.get('scheduler_id')
        if bound != scheduler_id or not (
                state == 'cancelled_bound' or
                (state == 'released' and isinstance(status.get('generation'), int) and status['generation'] > 0)):
            raise ReservationUncertain('terminal allocation release not confirmed')
        self.db.execute("UPDATE monthly_dispatch_holds SET state='CLOSED' WHERE job_id=? AND scheduler_id=? AND state IN ('BINDING','BOUND')", (job_id,scheduler_id))
        if self._row(job_id)['state'] != 'CLOSED':
            raise ReservationUncertain('terminal reservation changed concurrently')

    def bind(self, job_id, uid, scheduler_id, allow_released=False):
        if isinstance(scheduler_id, bool) or not isinstance(scheduler_id, int) or not 0 < scheduler_id <= 2**32-1:
            raise ReservationUncertain('invalid scheduler identity')
        row = self._row(job_id)
        if not row or row['uid'] != uid or row['state'] not in ('HELD', 'BINDING', 'BOUND'):
            raise ReservationUncertain('reservation intent unavailable')
        if row['scheduler_id'] not in (None, scheduler_id):
            raise ReservationUncertain('reservation bound to a different job')
        self.db.execute("UPDATE monthly_dispatch_holds SET state='BINDING',scheduler_id=? WHERE job_id=? AND state='HELD' AND scheduler_id IS NULL", (scheduler_id,job_id))
        row = self._row(job_id)
        if row['scheduler_id'] != scheduler_id:
            raise ReservationUncertain('reservation binding changed concurrently')
        month = int(month_now().replace('-', ''))
        status = self._status(row, rollover_month=month)
        if status.get('state') == 'bound' and status.get('scheduler_id') == scheduler_id and status['month'] < month:
            status = self._rollover(self._row(job_id), status, month)
        if status.get('state') == 'unbound' and status.get('scheduler_id') is None:
            if status['month'] < month:
                status = self._rollover(self._row(job_id), status, month)
            try:
                self.bridge.bind_reservation(uid, job_id, scheduler_id)
            except ControllerUnavailable as exc:
                raise ReservationUncertain('binding outcome unknown') from exc
        elif (status.get('state') == 'released' and allow_released
              and status.get('scheduler_id') == scheduler_id
              and isinstance(status.get('generation'), int) and status['generation'] > 0):
            pass  # The controller retained the terminal reservation identity.
        elif status.get('state') not in ('bound', 'prepared', 'active') or status.get('scheduler_id') != scheduler_id:
            raise ReservationUncertain('controller binding conflicts')
        self.db.execute("UPDATE monthly_dispatch_holds SET state='BOUND' WHERE job_id=? AND scheduler_id=? AND state='BINDING'", (job_id,scheduler_id))
