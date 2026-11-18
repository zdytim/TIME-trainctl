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
        # Acquire SQLite's writer lock before inspecting the schema, including
        # when the caller already owns a transaction. Concurrent processes
        # therefore cannot both decide to add the same column.
        db.execute('SAVEPOINT monthly_schema_migration')
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS monthly_dispatch_holds (
                job_id TEXT PRIMARY KEY, uid INTEGER NOT NULL, tier TEXT NOT NULL,
                gpu INTEGER NOT NULL, minutes INTEGER NOT NULL, month INTEGER NOT NULL,
                state TEXT NOT NULL, scheduler_id INTEGER)""")
            db.execute('UPDATE monthly_dispatch_holds SET state=state WHERE 0')
            columns = {row[1] for row in db.execute('PRAGMA table_info(monthly_dispatch_holds)')}
            if 'policy_version' not in columns:
                db.execute('ALTER TABLE monthly_dispatch_holds ADD COLUMN policy_version INTEGER NOT NULL DEFAULT 1')
            legacy_schema = 'bind_submit' not in columns and 'bind_sluid' not in columns
            for name in ('bind_submit', 'bind_sluid'):
                if name not in columns:
                    db.execute('ALTER TABLE monthly_dispatch_holds ADD COLUMN ' + name + ' TEXT')
            if 'binding_version' not in columns:
                db.execute('ALTER TABLE monthly_dispatch_holds ADD COLUMN binding_version INTEGER NOT NULL DEFAULT 0')
                # Only a pre-BIND2 schema proves a BOUND row is legacy.
                # In-flight bindings and partially migrated rows remain unknown.
                if legacy_schema:
                    db.execute("UPDATE monthly_dispatch_holds SET binding_version=1 WHERE state IN ('BOUND','CLOSED') AND scheduler_id IS NOT NULL")
                db.execute("UPDATE monthly_dispatch_holds SET binding_version=2 WHERE bind_submit IS NOT NULL AND bind_sluid IS NOT NULL")
                db.execute("UPDATE monthly_dispatch_holds SET binding_version=2 WHERE scheduler_id IS NULL AND state IN ('REQUESTING','HELD','DEFERRED','CANCELLED')")
            db.execute('RELEASE SAVEPOINT monthly_schema_migration')
        except BaseException:
            db.execute('ROLLBACK TO SAVEPOINT monthly_schema_migration')
            db.execute('RELEASE SAVEPOINT monthly_schema_migration')
            raise

    def _row(self, job_id):
        return self.db.execute('SELECT * FROM monthly_dispatch_holds WHERE job_id=?', (job_id,)).fetchone()

    @staticmethod
    def _validate(job_id, uid, tier, gpu, minutes, month, policy_version=1):
        if (not isinstance(job_id, str) or not re.fullmatch(r'[0-9a-f]{32}', job_id)
                or isinstance(uid, bool) or not isinstance(uid, int) or not 0 < uid <= 2**32-1
                or tier not in ('debug', 'important', 'normal')
                or isinstance(gpu, bool) or not isinstance(gpu, int) or not 0 < gpu <= 2**32-1
                or isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 < minutes <= 2**32-1
                or isinstance(month, bool) or not isinstance(month, int) or not 197001 <= month <= 999912
                or not 1 <= month % 100 <= 12 or (policy_version == 1 and gpu * minutes * 60 * 10**9 > 2**64-1)
                or (policy_version == 2 and (minutes != 4294967295 or gpu * 10**9 > 2**64-1))):
            raise ReservationUncertain('invalid reservation request')

    def _status(self, row, rollover_month=None):
        try:
            if row['policy_version'] == 1:
                status = self.bridge.reservation_status(row['uid'], row['job_id'])
            else:
                status = self.bridge.reservation_status(row['uid'], row['job_id'], policy_version=2)
        except ControllerUnavailable as exc:
            raise ReservationUncertain('reservation status unavailable') from exc
        if not status.get('present'):
            raise ReservationUncertain('reservation absent; outcome is unknown')
        if any(status.get(k) != row[k] for k in ('uid', 'tier', 'gpu', 'minutes')):
            raise ReservationUncertain('reservation fields differ from durable intent')
        if status.get('policy_version', 1) != row['policy_version']:
            raise ReservationUncertain('reservation policy differs from durable intent')
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

    def hold(self, job_id, uid, tier, gpu, minutes, month, require_waiting=False, policy_version=1):
        self._validate(job_id, uid, tier, gpu, minutes, month, policy_version)
        if policy_version not in (1, 2):
            raise ReservationUncertain('invalid reservation policy')
        # Persist the immutable request first. The controller treats a repeat
        # of the same request as idempotent and retains cancellation tombstones.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if require_waiting:
                job = self.db.execute('SELECT state,owner_uid,selected_tier,policy_version,requested_gpu,requested_minutes,temporary_application_id FROM jobs WHERE id=?', (job_id,)).fetchone()
                if (not job or job['state'] != 'WAITING_QUOTA' or job['owner_uid'] != uid or
                        job['selected_tier'] != tier or job['policy_version'] != policy_version or
                        job['requested_gpu'] != gpu or job['requested_minutes'] != minutes):
                    raise ReservationUncertain('job no longer waiting for this reservation')
                if job['temporary_application_id']:
                    app = self.db.execute('SELECT owner_uid,policy_version,bound_job_id,authorization_used FROM applications WHERE id=?',
                                          (job['temporary_application_id'],)).fetchone()
                    if (not app or app['owner_uid'] != uid or app['policy_version'] != policy_version or
                            app['bound_job_id'] != job_id or app['authorization_used'] != 1):
                        raise ReservationUncertain('application reservation policy differs')
            row = self._row(job_id)
            should_send = False
            if row is None:
                self.db.execute('INSERT INTO monthly_dispatch_holds(job_id,uid,tier,gpu,minutes,month,state,scheduler_id,binding_version,policy_version) VALUES(?,?,?,?,?,?,?,?,2,?)',
                                (job_id, uid, tier, gpu, minutes, month, 'REQUESTING', None, policy_version))
                should_send = True
            else:
                if tuple(row[k] for k in ('uid', 'tier', 'gpu', 'minutes', 'policy_version')) != (uid, tier, gpu, minutes, policy_version):
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
                if policy_version == 1:
                    observed = self.bridge.reservation_status(uid, job_id)
                else:
                    observed = self.bridge.reservation_status(uid, job_id, policy_version=2)
            except ControllerUnavailable as exc:
                raise ReservationUncertain('reservation status unavailable') from exc
            should_send = not observed.get('present')
        if should_send:
            try:
                if policy_version == 1:
                    self.bridge.reserve(uid, job_id, tier, gpu, minutes)
                else:
                    self.bridge.reserve(uid, job_id, tier, gpu, minutes, policy_version=policy_version)
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

    def cancel(self, job_id, uid, *, ensure_tombstone=False):
        """Cancel only after the caller fenced scheduler submission in SQLite."""
        row = self._row(job_id)
        if row is None:
            if ensure_tombstone:
                job = self.db.execute("SELECT owner_uid,state,scheduler_id,selected_tier,requested_gpu,requested_minutes,policy_version "
                                      "FROM jobs WHERE id=?", (job_id,)).fetchone()
                if (not job or job['owner_uid'] != uid or job['state'] != 'CANCELLING_RESERVATION' or
                        job['scheduler_id'] is not None or job['selected_tier'] is None):
                    raise ReservationUncertain('job is not fenced for cancellation')
                try:
                    if job['policy_version'] == 1:
                        self.bridge.cancel_request(uid, job_id, job['selected_tier'], job['requested_gpu'], job['requested_minutes'])
                    else:
                        self.bridge.cancel_request(uid, job_id, job['selected_tier'], job['requested_gpu'], job['requested_minutes'], policy_version=job['policy_version'])
                        status = self.bridge.reservation_status(uid, job_id, policy_version=job['policy_version'])
                        if (not status.get('present') or status.get('state') != 'cancelled' or
                                any(status.get(key) != job[column] for key, column in
                                    (('tier', 'selected_tier'), ('gpu', 'requested_gpu'), ('minutes', 'requested_minutes')))):
                            raise ReservationUncertain('cancellation tombstone not confirmed')
                except (ControllerUnavailable, ValueError) as exc:
                    raise ReservationUncertain('cancellation tombstone outcome unknown') from exc
            return  # No intent can be created after the caller's fence.
        if row['uid'] != uid or row['scheduler_id'] is not None or row['state'] not in ('REQUESTING', 'HELD', 'DEFERRED', 'CANCELLED'):
            raise ReservationUncertain('reservation cannot be safely cancelled')
        # One controller transaction either cancels an existing unbound hold
        # or creates an absent-request tombstone. This fences in-flight RESERVE1.
        try:
            if row['policy_version'] == 1:
                self.bridge.cancel_request(uid, job_id, row['tier'], row['gpu'], row['minutes'])
            else:
                self.bridge.cancel_request(uid, job_id, row['tier'], row['gpu'], row['minutes'], policy_version=row['policy_version'])
                status = self._status(row)
                if status.get('state') != 'cancelled':
                    raise ReservationUncertain('cancellation not confirmed')
        except (ControllerUnavailable, ValueError) as exc:
            raise ReservationUncertain('cancellation outcome unknown') from exc
        self.db.execute("UPDATE monthly_dispatch_holds SET state='CANCELLED' WHERE job_id=? AND state IN ('REQUESTING','HELD','DEFERRED','CANCELLED') AND scheduler_id IS NULL", (job_id,))
        if self._row(job_id)['state'] != 'CANCELLED':
            raise ReservationUncertain('reservation changed while cancelling')

    def finish(self, job_id, uid, scheduler_id, *, require_controller_confirmation=False):
        """Reconcile a terminal scheduler job without refunding live allocation."""
        if (type(require_controller_confirmation) is not bool or isinstance(scheduler_id, bool) or
                not isinstance(scheduler_id, int) or not 0 < scheduler_id <= 2**32-1):
            raise ReservationUncertain('invalid scheduler identity')
        row = self._row(job_id)
        if not row or row['uid'] != uid or row['scheduler_id'] not in (None, scheduler_id):
            raise ReservationUncertain('terminal reservation identity mismatch')
        if row['state'] == 'HELD' and row['scheduler_id'] is None:
            self.db.execute("UPDATE monthly_dispatch_holds SET state='BINDING',scheduler_id=? WHERE job_id=? AND state='HELD' AND scheduler_id IS NULL", (scheduler_id,job_id))
            row = self._row(job_id)
        if row['scheduler_id'] != scheduler_id:
            raise ReservationUncertain('terminal scheduler identity unavailable')
        def confirm_policy2_identity(current):
            if current['binding_version'] != 2 or not current['bind_submit'] or not current['bind_sluid']:
                raise ReservationUncertain('durable binding identity unavailable')
            try:
                submit, sluid = int(current['bind_submit']), int(current['bind_sluid'])
                if str(submit) != current['bind_submit'] or str(sluid) != current['bind_sluid'] or not 0 < submit <= 2**64-1 or not 0 < sluid <= 2**64-1:
                    raise ValueError('invalid binding identity')
                receipt = self.bridge.terminal_binding_receipt(uid, job_id, scheduler_id, submit, sluid)
                if (receipt['submit'], receipt['sluid']) != (submit, sluid):
                    raise ReservationUncertain('controller binding identity differs')
                return submit, sluid, receipt['terminal'], receipt['generation']
            except (ControllerUnavailable, ValueError, KeyError, TypeError) as exc:
                raise ReservationUncertain('controller binding identity unavailable') from exc
        if row['state'] == 'CLOSED':
            if require_controller_confirmation or row['policy_version'] == 2:
                status = self._status(row)
                if (status.get('scheduler_id') != scheduler_id or not
                        (status.get('state') == 'cancelled_bound' or
                         (status.get('state') == 'released' and type(status.get('generation')) is int and
                          status['generation'] > 0))):
                    raise ReservationUncertain('closed reservation is not confirmed by controller')
                if row['policy_version'] == 2:
                    _, _, terminal, generation = confirm_policy2_identity(row)
                    if terminal != (3 if status.get('state') == 'cancelled_bound' else 2) or (terminal == 2 and generation != status.get('generation')):
                        raise ReservationUncertain('terminal receipt state differs')
            return
        if row['state'] not in ('BINDING', 'BOUND'):
            raise ReservationUncertain('terminal reservation state unavailable')
        status = self._status(row, rollover_month=int(month_now().replace('-', '')))
        state, bound = status.get('state'), status.get('scheduler_id')
        if state in ('unbound', 'bound') and bound in (None, scheduler_id):
            try:
                if row['policy_version'] == 1:
                    self.bridge.cancel_bound_reservation(uid, job_id, scheduler_id)
                elif row['policy_version'] == 2 and state == 'bound' and bound == scheduler_id:
                    submit, sluid, terminal, generation = confirm_policy2_identity(row)
                    if terminal != 0 or generation != 0:
                        raise ReservationUncertain('job is not confirmed terminal')
                    self.bridge.cancel_bound_reservation2(uid, job_id, scheduler_id, submit, sluid)
                else:
                    raise ReservationUncertain('bound cancellation identity unavailable')
            except ControllerUnavailable as exc:
                raise ReservationUncertain('terminal reservation cancellation unconfirmed') from exc
            status = self._status(self._row(job_id), rollover_month=int(month_now().replace('-', '')))
            state, bound = status.get('state'), status.get('scheduler_id')
        if bound != scheduler_id or not (
                state == 'cancelled_bound' or
                (state == 'released' and isinstance(status.get('generation'), int) and status['generation'] > 0)):
            raise ReservationUncertain('terminal allocation release not confirmed')
        if row['policy_version'] == 2:
            _, _, terminal, generation = confirm_policy2_identity(row)
            if terminal != (3 if state == 'cancelled_bound' else 2) or (terminal == 2 and generation != status.get('generation')):
                raise ReservationUncertain('terminal receipt state differs')
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
        if row['binding_version'] not in (1, 2):
            raise ReservationUncertain('reservation binding version requires reconciliation')
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
        elif (status.get('state') == 'released' and allow_released
              and status.get('scheduler_id') == scheduler_id
              and isinstance(status.get('generation'), int) and status['generation'] > 0):
            pass
        elif status.get('state') not in ('bound', 'prepared', 'active') or status.get('scheduler_id') != scheduler_id:
            raise ReservationUncertain('controller binding conflicts')
        row = self._row(job_id)
        if row['binding_version'] == 1:
            # The old schema proves this was already durably bound before the
            # migration. Never create a new BIND1 or infer an unbound result.
            if (row['state'] != 'BOUND' or status.get('scheduler_id') != scheduler_id or
                    status.get('state') not in ('bound', 'prepared', 'active', 'released')):
                raise ReservationUncertain('legacy binding cannot be confirmed')
            return
        if status.get('state') in ('unbound', 'bound', 'prepared', 'active'):
            # BIND2 is idempotent only for the same pending Slurm instance. A
            # lost reply is retried; a changed or missing job remains fenced.
            try:
                receipt = (self.bridge.bind_reservation(uid, job_id, scheduler_id)
                           if status.get('state') == 'unbound'
                           else self.bridge.binding_receipt(uid, job_id, scheduler_id))
            except (ControllerUnavailable, ValueError) as exc:
                raise ReservationUncertain('binding outcome unknown') from exc
            submit, sluid = str(receipt['submit']), str(receipt['sluid'])
            if status.get('state') in ('prepared', 'active') and status.get('sluid') != receipt['sluid']:
                raise ReservationUncertain('active reservation identity conflicts')
            if row['bind_submit'] not in (None, submit) or row['bind_sluid'] not in (None, sluid):
                raise ReservationUncertain('binding receipt conflicts with durable identity')
            self.db.execute("UPDATE monthly_dispatch_holds SET bind_submit=?,bind_sluid=? WHERE job_id=? AND scheduler_id=? AND state IN ('BINDING','BOUND')",
                            (submit, sluid, job_id, scheduler_id))
            row = self._row(job_id)
            if row['bind_submit'] != submit or row['bind_sluid'] != sluid:
                raise ReservationUncertain('binding receipt persistence failed')
        else:
            # Never reconstruct an already-running identity from a job ID.
            if not row['bind_submit'] or not row['bind_sluid'] or str(status.get('sluid')) != row['bind_sluid']:
                raise ReservationUncertain('running reservation identity unavailable')
        self.db.execute("UPDATE monthly_dispatch_holds SET state='BOUND' WHERE job_id=? AND scheduler_id=? AND state='BINDING'", (job_id,scheduler_id))
