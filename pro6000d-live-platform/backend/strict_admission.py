"""Experimental pre-acceptance marker journal; not installed by startup.

The controller fence cannot yet be safely cleared and marker lifecycle is
incomplete. Do not enable this component for production scheduling.
"""
import datetime as dt
import hashlib
import hmac
import json
import re

from controller_bridge import ControllerOutcomeUnknown, ControllerUnavailable, PriorityBarrierWait
from top_pending_intent import TopPendingIntents


class StrictAdmission:
    def reservation_status_for_job(self, uid, marker):
        job = self.db.execute('SELECT owner_uid,policy_version FROM jobs WHERE id=?', (marker,)).fetchone()
        hold = self.db.execute('SELECT uid,policy_version FROM monthly_dispatch_holds WHERE job_id=?', (marker,)).fetchone()
        if job is not None and job['owner_uid'] != uid or hold is not None and hold['uid'] != uid:
            raise ControllerUnavailable('reservation owner differs')
        policy = job['policy_version'] if job is not None else (hold['policy_version'] if hold is not None else 1)
        if policy not in (1, 2) or hold is not None and hold['policy_version'] != policy:
            raise ControllerUnavailable('reservation policy differs')
        if policy == 1:
            return self.bridge.reservation_status(uid, marker)
        return self.bridge.reservation_status(uid, marker, policy_version=policy)

    def __init__(self, runtime, bridge, db):
        if runtime.store.db is not db or db.in_transaction:
            raise ValueError('idle shared broker database required')
        db.execute('PRAGMA synchronous=FULL')
        if db.execute('PRAGMA synchronous').fetchone()[0] < 2:
            raise ValueError('durable SQLite synchronization required')
        self.runtime, self.bridge, self.db = runtime, bridge, db
        self.intents = TopPendingIntents(db=db)
        db.execute('CREATE TABLE IF NOT EXISTS top_fence_intents ('
                   'marker TEXT PRIMARY KEY, uid INTEGER NOT NULL, username TEXT NOT NULL, '
                   'account_generation TEXT NOT NULL, request_id TEXT NOT NULL, request_hash TEXT NOT NULL, effective_digest TEXT NOT NULL, '
                   "state TEXT NOT NULL CHECK(state IN ('prepared','linked','closed')), UNIQUE(uid,request_id))")
        schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='top_fence_intents'").fetchone()
        columns = {row[1] for row in db.execute('PRAGMA table_info(top_fence_intents)')}
        if schema is None or "'closed'" not in schema[0] or 'effective_digest' not in columns:
            raise ValueError('priority fence schema requires reviewed migration')
        with runtime.coordinator.serialized():
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('CREATE TABLE IF NOT EXISTS priority_lane_revision '
                           '(singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL CHECK(revision>=0))')
                db.execute('INSERT OR IGNORE INTO priority_lane_revision VALUES(1,0)')
                revision = db.execute('SELECT revision FROM priority_lane_revision WHERE singleton=1').fetchone()
                if revision is None or type(revision[0]) is not int or revision[0] < 0:
                    raise ValueError('priority queue revision schema is invalid')
                db.execute('DROP TRIGGER IF EXISTS priority_lane_revision_update')
                db.execute('DROP TRIGGER IF EXISTS priority_lane_promote_revision')
                db.execute('CREATE TRIGGER priority_lane_promote_revision '
                           'AFTER UPDATE OF priority_marker_required ON jobs '
                           'WHEN OLD.priority_marker_required=0 AND NEW.priority_marker_required=1 '
                           'BEGIN UPDATE priority_lane_revision SET revision=revision+1 WHERE singleton=1; END')
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise
        db.execute("CREATE TABLE IF NOT EXISTS top_submitted_cancel_intents ("
                   "marker TEXT PRIMARY KEY, uid INTEGER NOT NULL, scheduler_id INTEGER NOT NULL, "
                   "submit_time INTEGER NOT NULL, sluid TEXT NOT NULL, "
                   "state TEXT NOT NULL CHECK(state IN ('requested','reservation_cancelled')))" )
        db.execute('CREATE TABLE IF NOT EXISTS top_terminal_receipts ('
                   'marker TEXT PRIMARY KEY, uid INTEGER NOT NULL, scheduler_id INTEGER NOT NULL, '
                   'submit_time INTEGER NOT NULL, sluid TEXT NOT NULL, generation INTEGER NOT NULL, '
                   'final_state INTEGER NOT NULL, exit_code INTEGER NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS top_started_receipts ('
                   'marker TEXT PRIMARY KEY, uid INTEGER NOT NULL, scheduler_id INTEGER NOT NULL, '
                   'submit_time INTEGER NOT NULL, sluid TEXT NOT NULL, generation INTEGER NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS top_marker_bindings ('
                   'marker TEXT PRIMARY KEY, uid INTEGER NOT NULL, scheduler_id INTEGER NOT NULL, '
                   'submit_time INTEGER NOT NULL, sluid TEXT NOT NULL, restart INTEGER NOT NULL, '
                   'generation INTEGER NOT NULL)')
        db.execute("CREATE TABLE IF NOT EXISTS top_promotion_batches ("
                   "request_id TEXT PRIMARY KEY, uid INTEGER NOT NULL, username TEXT NOT NULL, "
                   "account_generation TEXT NOT NULL, expected_version INTEGER NOT NULL, "
                   "actor_uid INTEGER NOT NULL, actor_name TEXT NOT NULL, "
                   "member_count INTEGER NOT NULL, members_digest TEXT NOT NULL, "
                   "state TEXT NOT NULL CHECK(state IN ('prepared','fenced','policy','complete')))" )
        batch_columns = {row[1] for row in db.execute('PRAGMA table_info(top_promotion_batches)')}
        if not {'member_count', 'members_digest'}.issubset(batch_columns):
            raise ValueError('priority promotion schema requires reviewed migration')
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS top_promotion_one_open_uid ON top_promotion_batches(uid) WHERE state!='complete'")
        db.execute("CREATE TABLE IF NOT EXISTS top_promotion_members ("
                   "request_id TEXT NOT NULL, marker TEXT NOT NULL, client_request_id TEXT NOT NULL, "
                   "client_request_hash TEXT NOT NULL, effective_digest TEXT NOT NULL, "
                   "PRIMARY KEY(request_id,marker))")
        # Serialize additive migration across all broker processes. Existing
        # rows retain v1 semantics and the original five-field digest.
        with runtime.coordinator.serialized():
            db.execute('BEGIN IMMEDIATE')
            try:
                columns = {row[1] for row in db.execute('PRAGMA table_info(top_promotion_members)')}
                for name, declaration in (
                    ('identity_version', 'INTEGER NOT NULL DEFAULT 1'),
                    ('scheduler_id', 'INTEGER'), ('submit_time', 'INTEGER'),
                    ('sluid', 'TEXT'), ('restart', 'INTEGER'),
                    ('reservation_json', 'TEXT'), ('classification', 'INTEGER')):
                    if name not in columns:
                        db.execute('ALTER TABLE top_promotion_members ADD COLUMN ' + name + ' ' + declaration)
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise
        db.execute("CREATE TABLE IF NOT EXISTS top_pending_promotion_v2 ("
                   "request_id TEXT PRIMARY KEY, uid INTEGER NOT NULL, identity_json TEXT NOT NULL, "
                   "members_json TEXT NOT NULL, digest TEXT NOT NULL, "
                   "state TEXT NOT NULL CHECK(state IN ('fence_requested','quarantined')))")
        db.execute("CREATE TABLE IF NOT EXISTS top_pending_promotion_v2_observed ("
                   "request_id TEXT NOT NULL, marker TEXT NOT NULL, uid INTEGER NOT NULL, "
                   "scheduler_id INTEGER NOT NULL, submit_time INTEGER NOT NULL, sluid TEXT NOT NULL, "
                   "restart INTEGER NOT NULL, generation TEXT NOT NULL, state TEXT NOT NULL "
                   "CHECK(state IN ('pending','running')), PRIMARY KEY(request_id,marker))")
        db.execute("CREATE TABLE IF NOT EXISTS top_pending_promotion_v2_waiting ("
                   "request_id TEXT NOT NULL, marker TEXT NOT NULL, member_json TEXT NOT NULL, "
                   "PRIMARY KEY(request_id,marker))")
        db.execute("CREATE TABLE IF NOT EXISTS top_pending_promotion_v2_policy ("
                   "request_id TEXT PRIMARY KEY, identity_json TEXT NOT NULL, "
                   "state TEXT NOT NULL CHECK(state IN ('policy_requested','policy_committed')))")
        db.execute("CREATE TABLE IF NOT EXISTS top_pending_promotion_v2_adopted ("
                   "request_id TEXT PRIMARY KEY, members_digest TEXT NOT NULL, "
                   "state TEXT NOT NULL CHECK(state='adopted'))")
        db.execute("CREATE TABLE IF NOT EXISTS top_pending_promotion_v2_completed ("
                   "request_id TEXT PRIMARY KEY, uid INTEGER NOT NULL, identity_json TEXT NOT NULL, "
                   "members_json TEXT NOT NULL, members_digest TEXT NOT NULL, epoch TEXT NOT NULL, "
                   "boot TEXT NOT NULL, watermark TEXT NOT NULL, ledger_seq TEXT NOT NULL, snapshot_digest TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS top_pending_promotion_v2_clear_attempt ("
                   "request_id TEXT PRIMARY KEY, members_digest TEXT NOT NULL, "
                   "state TEXT NOT NULL CHECK(state='outcome_unknown'))")
        db.execute("CREATE TABLE IF NOT EXISTS top_pending_promotion_v2_terminal ("
                   "request_id TEXT NOT NULL, marker TEXT NOT NULL, uid INTEGER NOT NULL, "
                   "scheduler_id INTEGER NOT NULL, submit_time INTEGER NOT NULL, sluid TEXT NOT NULL, "
                   "generation TEXT NOT NULL, final_state INTEGER NOT NULL, exit_code INTEGER NOT NULL, "
                   "PRIMARY KEY(request_id,marker))")
        db.execute("CREATE TABLE IF NOT EXISTS top_pending_promotion_v2_cancelled ("
                   "request_id TEXT NOT NULL, marker TEXT NOT NULL, uid INTEGER NOT NULL, "
                   "scheduler_id INTEGER NOT NULL, submit_time INTEGER NOT NULL, sluid TEXT NOT NULL, "
                   "PRIMARY KEY(request_id,marker))")
        # Staging is separate from executable v1 batches: recovery must never
        # mistake a submitted-job proof for authorization to mutate policy.
        db.execute("CREATE TABLE IF NOT EXISTS top_promotion_pending_proofs ("
                   "request_id TEXT NOT NULL, marker TEXT NOT NULL, uid INTEGER NOT NULL, "
                   "scheduler_id INTEGER NOT NULL, submit_time INTEGER NOT NULL, sluid TEXT NOT NULL, "
                   "restart INTEGER NOT NULL, reservation_json TEXT NOT NULL, classification INTEGER NOT NULL, "
                   "proof_digest TEXT NOT NULL, PRIMARY KEY(request_id,marker))")
        db.execute("CREATE TABLE IF NOT EXISTS top_promotion_cancellations ("
                   "request_id TEXT NOT NULL, marker TEXT NOT NULL, target TEXT NOT NULL "
                   "CHECK(target IN ('CANCELLED','EXPIRED')), state TEXT NOT NULL "
                   "CHECK(state IN ('requested','finished')), PRIMARY KEY(request_id,marker))")
        db.execute("CREATE TABLE IF NOT EXISTS top_orphan_cancellations ("
                   "marker TEXT PRIMARY KEY, uid INTEGER NOT NULL, username TEXT NOT NULL, "
                   "account_generation TEXT NOT NULL, request_id TEXT NOT NULL, request_hash TEXT NOT NULL, "
                   "effective_digest TEXT NOT NULL, target TEXT NOT NULL CHECK(target IN ('CANCELLED','EXPIRED')), "
                   "state TEXT NOT NULL CHECK(state IN ('requested','finished')))" )
        self.reservation_cancel = self._promotion_reservation_cancel
        if getattr(runtime, 'admission_guard', None) is not None or getattr(runtime, 'promotion_handler', None) is not None:
            raise ValueError('priority admission guard already installed')
        runtime.admission_guard = self._has_unresolved_for_uid
        runtime.promotion_handler = self

    @staticmethod
    def encode_shared_priority_snapshot(rows):
        """Encode common marker identities as ASCII lines, version 1.

        Each row is (uid, marker, account generation, request id, request hash,
        lifecycle, job id, submit time, sluid, restart, allocation generation).
        Numeric values are canonical unsigned decimal; zero denotes no binding.
        """
        normalized = []
        for row in rows:
            if len(row) != 11:
                raise ControllerUnavailable('invalid shared priority identity')
            uid, marker, account, request, digest, state, job, submit, sluid, restart, generation = row
            if (type(uid) is not int or not 0 <= uid <= 0xffffffff or
                    not isinstance(marker, str) or not re.fullmatch(r'[0-9a-f]{32}', marker) or
                    not isinstance(account, str) or not re.fullmatch(r'[0-9a-f]{32}', account) or
                    not isinstance(request, str) or not re.fullmatch(r'[A-Za-z0-9-]{8,80}', request) or
                    not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or
                    state not in ('pending', 'bound', 'running', 'terminal', 'cancelled', 'cancelled_bound') or
                    any(type(x) is not int or not 0 <= x <= 2**64-1
                        for x in (job, submit, sluid, restart, generation)) or
                    job > 0xffffffff or restart > 0xffffffff or
                    (state == 'pending' and any((job, submit, sluid, restart, generation))) or
                    (state in ('bound', 'cancelled_bound') and (not job or not submit or not sluid or restart or generation)) or
                    (state in ('running', 'terminal') and (not job or not submit or not sluid or not generation))):
                raise ControllerUnavailable('invalid shared priority identity')
            normalized.append((marker, '%d|%s|%s|%s|%s|%s|%d|%d|%d|%d|%d\n' %
                               (uid, marker, account, request, digest, state, job, submit, sluid, restart, generation)))
        normalized.sort(key=lambda item: item[0])
        if any(normalized[i-1][0] == normalized[i][0] for i in range(1, len(normalized))):
            raise ControllerUnavailable('duplicate shared priority marker')
        return ('TOP-SNAPSHOT-1\n' + ''.join(item[1] for item in normalized)).encode('ascii')

    def fence_snapshot_locked(self, *, include_adopted_v2=False):
        """Snapshot candidate intents while excluding concurrent database writers."""
        self.runtime.coordinator.assert_serialized()
        if self.db.in_transaction:
            raise ControllerUnavailable('priority snapshot requires idle database')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            return self._fence_snapshot_in_transaction(include_adopted_v2=include_adopted_v2)
        finally:
            self.db.execute('ROLLBACK')

    def _fence_snapshot_in_transaction(self, submitting_marker=None, *, include_adopted_v2=False):
        """Build a stable candidate snapshot; it does not authorize a clear.

        Caller holds the cross-process coordinator lock. Unresolved policy or
        cancellation operations make the snapshot unavailable.
        """
        self.runtime.coordinator.assert_serialized()
        if not self.db.in_transaction or not self.runtime.reconciled_for_current_boot():
            raise ControllerUnavailable('priority snapshot requires reconciled transaction')
        if self.runtime.store.outbox.pending() is not None or self.runtime.store.pending_confirmations():
            raise ControllerUnavailable('priority policy mutation is unresolved')
        batches_v2 = self.db.execute('SELECT request_id,uid,identity_json,members_json,digest,state FROM top_pending_promotion_v2').fetchall()
        if batches_v2 and not include_adopted_v2:
            raise ControllerUnavailable('priority v2 snapshot requires explicit audit')
        if include_adopted_v2:
            self._validate_adopted_v2_snapshot(batches_v2)
        if (self.db.execute("SELECT 1 FROM top_promotion_batches WHERE state!='complete' LIMIT 1").fetchone() or
                self.db.execute("SELECT 1 FROM top_promotion_cancellations WHERE state!='finished' LIMIT 1").fetchone() or
                self.db.execute("SELECT 1 FROM top_orphan_cancellations WHERE state!='finished' LIMIT 1").fetchone() or
                self.db.execute("SELECT 1 FROM top_pending_intents WHERE state NOT IN ('registered','cancelled') LIMIT 1").fetchone() or
                self.db.execute("SELECT 1 FROM top_fence_intents WHERE state='prepared' LIMIT 1").fetchone()):
            raise ControllerUnavailable('priority task mutation is unresolved')
        status = self.runtime.coordinator.transport.status()
        if (not isinstance(status, dict) or not isinstance(status.get('epoch'), str) or
                not re.fullmatch(r'[0-9a-f]{32}', status['epoch']) or
                not isinstance(status.get('boot'), str) or not re.fullmatch(r'[0-9a-f]{32}', status['boot']) or
                type(status.get('watermark')) is not int or not 0 <= status['watermark'] <= 2**64-1 or
                type(status.get('ledger_seq')) is not int or not 0 <= status['ledger_seq'] <= 2**64-1):
            raise ControllerUnavailable('priority controller identity unavailable')
        if self.db.execute(
                "SELECT 1 FROM jobs j LEFT JOIN top_pending_intents i ON i.marker=j.id "
                "WHERE j.priority_marker_required=1 AND j.state IN "
                "('WAITING_QUOTA','SUBMITTING','PENDING','RUNNING','CANCELLING_RESERVATION') "
                "AND i.marker IS NULL LIMIT 1").fetchone():
            raise ControllerUnavailable('priority job is missing its durable intent')
        rows = [tuple(row) for row in self.db.execute(
            'SELECT i.marker,i.uid,i.account_generation,i.request_id,i.request_hash,i.state,'
            'f.effective_digest,f.state,j.state,j.scheduler_id '
            'FROM top_pending_intents i LEFT JOIN top_fence_intents f ON f.marker=i.marker '
            'LEFT JOIN jobs j ON j.id=i.marker ORDER BY i.marker')]
        if any(row[6] is None or row[7] not in ('linked', 'closed') or row[8] is None for row in rows):
            raise ControllerUnavailable('priority snapshot has incomplete intent')
        shared = []
        for marker, uid, account, request, digest, intent_state, effective, fence_state, job_state, scheduler_id in rows:
            binding = self.db.execute(
                'SELECT uid,scheduler_id,submit_time,sluid,restart,generation FROM top_marker_bindings WHERE marker=?',
                (marker,)).fetchone()
            if intent_state == 'cancelled':
                if fence_state != 'closed':
                    raise ControllerUnavailable('cancelled priority fence differs')
                if binding is None and scheduler_id is None:
                    lifecycle, identity = 'cancelled', (0, 0, 0, 0, 0)
                elif binding is not None and scheduler_id is not None:
                    owner, job, submit, sluid_text, restart, generation = tuple(binding)
                    cancel = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,state FROM top_submitted_cancel_intents WHERE marker=?', (marker,)).fetchone()
                    if (owner != uid or str(job) != str(scheduler_id) or restart or generation or cancel is None or
                            tuple(cancel) != (uid, job, submit, sluid_text, 'reservation_cancelled')):
                        raise ControllerUnavailable('cancelled bound identity differs')
                    lifecycle, identity = 'cancelled_bound', (job, submit, int(sluid_text), 0, 0)
                else:
                    raise ControllerUnavailable('cancelled priority identity is ambiguous')
            elif binding is None:
                if (fence_state != 'linked' or scheduler_id is not None or
                        not (job_state == 'WAITING_QUOTA' or
                             (job_state == 'SUBMITTING' and marker == submitting_marker))):
                    raise ControllerUnavailable('unbound priority identity is ambiguous')
                lifecycle, identity = 'pending', (0, 0, 0, 0, 0)
            else:
                owner, job, submit, sluid_text, restart, generation = tuple(binding)
                if (owner != uid or scheduler_id is None or str(scheduler_id) != str(job) or
                        not isinstance(sluid_text, str) or not re.fullmatch(r'[1-9][0-9]*', sluid_text)):
                    raise ControllerUnavailable('priority binding identity differs')
                if job_state in ('PENDING', 'SUBMITTING') and generation == 0:
                    lifecycle = 'bound'
                elif job_state in ('RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED') and generation > 0:
                    started = self.db.execute(
                        'SELECT uid,scheduler_id,submit_time,sluid,generation FROM top_started_receipts WHERE marker=?',
                        (marker,)).fetchone()
                    if started is None or tuple(started) != (uid, job, submit, sluid_text, generation):
                        raise ControllerUnavailable('running priority receipt missing or conflicting')
                    if job_state != 'RUNNING':
                        terminal_receipt = self.db.execute(
                            'SELECT uid,scheduler_id,submit_time,sluid,generation,final_state FROM top_terminal_receipts WHERE marker=?',
                            (marker,)).fetchone()
                        if (terminal_receipt is None or tuple(terminal_receipt[:5]) != (uid, job, submit, sluid_text, generation) or
                                {3: 'COMPLETED', 4: 'CANCELLED', 5: 'FAILED', 6: 'FAILED', 7: 'FAILED', 8: 'FAILED', 9: 'FAILED', 10: 'FAILED', 11: 'FAILED'}.get(terminal_receipt[5] & 0xff) != job_state):
                            raise ControllerUnavailable('terminal priority receipt missing or conflicting')
                    lifecycle = 'running' if job_state == 'RUNNING' else 'terminal'
                else:
                    raise ControllerUnavailable('priority lifecycle requires reconciliation')
                identity = (job, submit, int(sluid_text), restart, generation)
            shared.append((uid, marker, account, request, digest, lifecycle, *identity))
        encoded = self.encode_shared_priority_snapshot(shared)
        payload = {'version': 1, 'epoch': status['epoch'], 'boot': status['boot'],
                   'watermark': status['watermark'], 'ledger_seq': status['ledger_seq'], 'intents': shared}
        if self.runtime.coordinator.transport.status() != status or not self.runtime.reconciled_for_current_boot():
            raise ControllerUnavailable('priority controller changed during snapshot')
        return payload, hashlib.sha256(encoded).hexdigest()

    def _verify_v2_terminal_locked(self, request_id, marker, uid, sid):
        """Require the exact durable terminal observation and controller proof."""
        self.runtime.coordinator.assert_serialized()
        observation = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,generation,final_state,exit_code FROM top_pending_promotion_v2_terminal WHERE request_id=? AND marker=?', (request_id, marker)).fetchone()
        if observation is None:
            raise ControllerUnavailable('v2 terminal observation unavailable')
        owner, job_id, submit, sluid, generation_text, final_state, exit_code = tuple(observation)
        if (owner != uid or str(job_id) != str(sid) or not isinstance(generation_text, str) or
                not re.fullmatch(r'[1-9][0-9]*', generation_text)):
            raise ControllerUnavailable('v2 terminal observation identity differs')
        generation = int(generation_text)
        binding = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,restart,generation FROM top_marker_bindings WHERE marker=?', (marker,)).fetchone()
        terminal = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,generation,final_state,exit_code FROM top_terminal_receipts WHERE marker=?', (marker,)).fetchone()
        hold = self.db.execute('SELECT uid,scheduler_id,state,binding_version,bind_submit,bind_sluid FROM monthly_dispatch_holds WHERE job_id=?', (marker,)).fetchone()
        job = self.db.execute('SELECT owner_uid,scheduler_id,state,priority_marker_required FROM jobs WHERE id=?', (marker,)).fetchone()
        state = {3:'COMPLETED',4:'CANCELLED',5:'FAILED',6:'FAILED',7:'FAILED',8:'FAILED',9:'FAILED',10:'FAILED',11:'FAILED'}.get(final_state & 0xff)
        expected = (uid, job_id, submit, sluid, generation, final_state, exit_code)
        if (state is None or binding is None or tuple(binding) != (uid, job_id, submit, sluid, 0, generation) or
                terminal is None or tuple(terminal) != expected or hold is None or
                tuple(hold) != (uid, job_id, 'CLOSED', 2, str(submit), sluid) or job is None or
                tuple(job) != (uid, str(job_id), state, 1) or not self._has_marker_locked(marker)):
            raise ControllerUnavailable('v2 terminal durable identity differs')
        remote = self.bridge.top_terminal_receipt(uid, marker, job_id)
        reservation = self.reservation_status_for_job(uid, marker)
        if (remote != {'submit': submit, 'sluid': int(sluid), 'generation': generation, 'state': final_state, 'exit_code': exit_code} or
                reservation.get('state') != 'released' or reservation.get('scheduler_id') != job_id or
                reservation.get('generation') != generation or reservation.get('sluid') != int(sluid)):
            raise ControllerUnavailable('v2 terminal controller proof differs')
        return expected

    def _verify_v2_finished_locked(self, request_id, marker, uid, sid):
        """Verify either a terminal allocation or a never-started cancellation."""
        self.runtime.coordinator.assert_serialized()
        cancelled = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid FROM top_pending_promotion_v2_cancelled WHERE request_id=? AND marker=?', (request_id, marker)).fetchone()
        if cancelled is None:
            return self._verify_v2_terminal_locked(request_id, marker, uid, sid)
        owner, job_id, submit, sluid = tuple(cancelled)
        binding = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,restart,generation FROM top_marker_bindings WHERE marker=?', (marker,)).fetchone()
        intent = self.db.execute('SELECT uid,account_generation,request_id,request_hash,state FROM top_pending_intents WHERE marker=?', (marker,)).fetchone()
        journal = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,state FROM top_submitted_cancel_intents WHERE marker=?', (marker,)).fetchone()
        hold = self.db.execute('SELECT uid,scheduler_id,state,binding_version,bind_submit,bind_sluid FROM monthly_dispatch_holds WHERE job_id=?', (marker,)).fetchone()
        job = self.db.execute('SELECT owner_uid,scheduler_id,state,priority_marker_required FROM jobs WHERE id=?', (marker,)).fetchone()
        if (owner != uid or str(job_id) != str(sid) or binding is None or tuple(binding) != (uid,job_id,submit,sluid,0,0) or
                intent is None or intent[0] != uid or intent[4] != 'cancelled' or journal is None or
                tuple(journal) != (uid,job_id,submit,sluid,'reservation_cancelled') or hold is None or
                tuple(hold) != (uid,job_id,'CLOSED',2,str(submit),sluid) or job is None or
                tuple(job) != (uid,str(job_id),'CANCELLED',1) or not self._has_marker_locked(marker) or
                self.db.execute('SELECT 1 FROM top_started_receipts WHERE marker=?', (marker,)).fetchone()):
            raise ControllerUnavailable('cancelled promotion identity differs')
        self.bridge.cancelled_top_receipt(marker,uid,intent[1],intent[2],intent[3],job_id,submit,int(sluid))
        reservation = self.reservation_status_for_job(uid,marker)
        if reservation.get('state') != 'cancelled_bound' or reservation.get('scheduler_id') != job_id or reservation.get('generation') is not None:
            raise ControllerUnavailable('cancelled reservation differs')
        return (uid,job_id,submit,sluid,0,None,None)

    def _validate_adopted_v2_snapshot(self, batches):
        """Validate v2 members without mutating policy, markers or the fence."""
        self.runtime.coordinator.assert_serialized()
        if not self.db.in_transaction:
            raise ControllerUnavailable('v2 snapshot requires a transaction')
        seen = set()
        status = self.runtime.coordinator.transport.status()
        for request_id, uid, identity_json, members_json, digest, state in batches:
            if (state != 'quarantined' or not isinstance(digest, str) or
                    not hmac.compare_digest(hashlib.sha256(members_json.encode('utf-8')).hexdigest(), digest)):
                raise ControllerUnavailable('v2 batch identity differs')
            identity, snapshot = json.loads(identity_json), json.loads(members_json)
            if not isinstance(identity, list) or len(identity) != 6 or identity[0] != uid or not isinstance(snapshot, dict) or snapshot.get('version') != 2:
                raise ControllerUnavailable('v2 batch metadata invalid')
            adopted = self.db.execute('SELECT members_digest,state FROM top_pending_promotion_v2_adopted WHERE request_id=?', (request_id,)).fetchone()
            phase = self.db.execute('SELECT identity_json,state FROM top_pending_promotion_v2_policy WHERE request_id=?', (request_id,)).fetchone()
            if adopted is None or tuple(adopted) != (digest, 'adopted') or phase is None or tuple(phase) != (identity_json, 'policy_committed'):
                raise ControllerUnavailable('v2 adoption or policy incomplete')
            policy = self.runtime.store.get(uid)
            expected = {'uid': uid, 'username': identity[1], 'identity_generation': identity[2], 'enabled': True, 'version': identity[3] + 1}
            if (policy is None or any(policy.get(k) != v for k, v in expected.items()) or policy.get('pending_id') is not None or
                    self.runtime.coordinator.transport.user_status(status, uid, identity[1], identity[2]) != expected):
                raise ControllerUnavailable('v2 policy not verified')
            rows = snapshot.get('rows')
            if not isinstance(rows, list) or not rows:
                raise ControllerUnavailable('v2 members unavailable')
            proof_rows = snapshot.get('proofs')
            if not isinstance(proof_rows, list) or any(not isinstance(p, list) or len(p) != 9 for p in proof_rows):
                raise ControllerUnavailable('v2 proofs invalid')
            proofs = {p[0]: p for p in proof_rows}
            if (len(proofs) != len(proof_rows) or
                    set(proofs) != {m[0] for m in rows if isinstance(m, list) and len(m) == 12 and m[8] == 'PENDING'}):
                raise ControllerUnavailable('v2 proof coverage differs')
            for member in rows:
                if not isinstance(member, list) or len(member) != 12 or member[0] in seen:
                    raise ControllerUnavailable('v2 member invalid or duplicated')
                marker, username, client_id, client_hash, effective, version, classification, tier, initial, origin, sid, cancel = member
                seen.add(marker)
                job = self.db.execute('SELECT owner_uid,owner_name,client_request_id,client_request_hash,effective_job_digest,effective_job_digest_version,priority_marker_required,selected_tier,state,origin,scheduler_id,cancel_target FROM jobs WHERE id=?', (marker,)).fetchone()
                if (job is None or tuple(job[:6]) != (uid, username, client_id, client_hash, effective, version) or
                        job[7] != tier or job[9] != origin or job[10] != sid or job[11] != cancel or
                        not self._verify_effective_job(uid, marker, effective)):
                    raise ControllerUnavailable('v2 member changed')
                if initial == 'WAITING_QUOTA':
                    observation = self.db.execute('SELECT member_json FROM top_pending_promotion_v2_waiting WHERE request_id=? AND marker=?', (request_id, marker)).fetchone()
                    if (job[8] != 'WAITING_QUOTA' or job[6] != 1 or observation is None or
                            observation[0] != json.dumps(member, separators=(',', ':'), allow_nan=False) or not self._has_marker_locked(marker)):
                        raise ControllerUnavailable('v2 waiting member not adopted')
                elif initial == 'PENDING':
                    proof = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,restart,generation,state FROM top_pending_promotion_v2_observed WHERE request_id=? AND marker=?', (request_id, marker)).fetchone()
                    staged = proofs[marker]
                    if (proof is None or proof[0] != uid or str(proof[1]) != sid or
                            tuple(proof[:5]) != tuple(staged[1:6]) or not isinstance(staged[6], str) or
                            not hmac.compare_digest(hashlib.sha256(staged[6].encode('utf-8')).hexdigest(), staged[8])):
                        raise ControllerUnavailable('v2 submitted observation unavailable')
                    if job[8] in ('COMPLETED', 'CANCELLED', 'FAILED'):
                        terminal_identity = self._verify_v2_finished_locked(request_id, marker, uid, sid)
                        if tuple(proof[:4]) != terminal_identity[:4]:
                            raise ControllerUnavailable('v2 terminal original observation differs')
                    elif proof[6] == 'pending':
                        binding = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,restart,generation FROM top_marker_bindings WHERE marker=?', (marker,)).fetchone()
                        if job[8] != 'PENDING' or job[6] != 1 or binding is None or tuple(binding) != tuple(proof[:5]) + (0,) or not self._has_marker_locked(marker):
                            raise ControllerUnavailable('v2 submitted member not adopted')
                    elif proof[6] == 'running':
                        if job[8] != 'RUNNING' or job[6] not in (0, 1) or proof[4] != 0 or not proof[5] or proof[5] == '0':
                            raise ControllerUnavailable('v2 running member requires reconciliation')
                        if job[6] == 1:
                            binding = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,restart,generation FROM top_marker_bindings WHERE marker=?', (marker,)).fetchone()
                            started = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,generation FROM top_started_receipts WHERE marker=?', (marker,)).fetchone()
                            if (not self._has_marker_locked(marker) or binding is None or tuple(binding[:5]) != tuple(proof[:5]) or
                                    str(binding[5]) != proof[5] or started is None or
                                    tuple(started) != (uid, proof[1], proof[2], proof[3], binding[5])):
                                raise ControllerUnavailable('v2 adopted running receipt differs')
                            live = self.bridge.top_running_receipt(uid, marker, proof[1])
                        else:
                            live = self.bridge.ordinary_running_receipt(uid, marker, proof[1])
                        if (live['submit'] != proof[2] or str(live['sluid']) != proof[3] or str(live['generation']) != proof[5]):
                            raise ControllerUnavailable('v2 running identity differs')
                    else:
                        raise ControllerUnavailable('v2 lifecycle unresolved')
                else:
                    raise ControllerUnavailable('v2 lifecycle unsupported')
        if self.runtime.coordinator.transport.status() != status:
            raise ControllerUnavailable('controller changed during v2 snapshot audit')

    def clear_uncertainty_after_reconciliation(self):
        """Compare the locked durable snapshot with the same controller boot."""
        with self.runtime.coordinator.serialized():
            self.db.execute('BEGIN IMMEDIATE')
            try:
                payload, digest = self._fence_snapshot_in_transaction()
                self._same_controller()
                before = self.runtime.coordinator.transport.status()
                if any(before.get(k) != payload[k] for k in ('epoch', 'boot', 'watermark', 'ledger_seq')):
                    raise ControllerUnavailable('controller changed before uncertainty clear')
                self.bridge.clear_top_uncertainty(payload['epoch'], payload['boot'], payload['watermark'], payload['ledger_seq'], digest)
                # The audited snapshot query accepts either pending-barrier
                # state and verifies the same boot, epoch and watermark under
                # JOB_WRITE. ORDERED_STATUS2 instead requires a closed fence.
                after_digest = self.bridge.top_snapshot_digest(payload['epoch'], payload['boot'], payload['watermark'])
                if not hmac.compare_digest(after_digest, digest):
                    raise ControllerOutcomeUnknown('controller changed after uncertainty clear')
            except BaseException:
                try:
                    self.bridge.fence_top()
                except Exception:
                    pass
                raise
            finally:
                self.db.execute('ROLLBACK')

    def submit_creation_bound_top(self, uid, marker, submit):
        """Hold the broker coordinator from the audited clear through sbatch.

        A failed or ambiguous attestation leaves the submission unattempted;
        the caller restores the waiting claim. Controller-side creation
        binding and the final gate still decide admission after this check.
        """
        with self.runtime.coordinator.serialized():
            self.db.execute('BEGIN IMMEDIATE')
            try:
                row = self.db.execute(
                    "SELECT owner_uid,state,scheduler_id FROM jobs WHERE id=?", (marker,)).fetchone()
                if row is None or tuple(row) != (uid, 'SUBMITTING', None):
                    raise ControllerUnavailable('submission claim changed')
                payload, digest = self._fence_snapshot_in_transaction(submitting_marker=marker)
                self._same_controller()
                self.bridge.clear_top_uncertainty(payload['epoch'], payload['boot'], payload['watermark'], payload['ledger_seq'], digest)
                if not self.bridge.supports_creation_bound_top(
                        payload['epoch'], payload['boot'], payload['watermark'], digest, uid, marker):
                    raise ControllerUnavailable('creation-bound capability unavailable')
                if not hmac.compare_digest(self.bridge.top_snapshot_digest(
                        payload['epoch'], payload['boot'], payload['watermark']), digest):
                    raise ControllerUnavailable('priority snapshot changed before submission')
            except BaseException:
                try:
                    self.bridge.fence_top()
                except Exception:
                    pass
                raise
            finally:
                self.db.execute('ROLLBACK')
            # Coordinator remains held; do not retry an ambiguous sbatch result.
            return submit()

    def restore_unsubmitted_claim(self, submitted):
        """Undo only a claim whose scheduler call was never attempted."""
        marker, uid = submitted['id'], submitted['owner_uid']
        with self.runtime.coordinator.serialized():
            self.db.execute('BEGIN IMMEDIATE')
            try:
                current = self.db.execute('SELECT * FROM jobs WHERE id=?', (marker,)).fetchone()
                if (current is None or dict(current) != dict(submitted) or
                        current['state'] != 'SUBMITTING' or current['scheduler_id'] is not None or
                        not self._has_marker_locked(marker)):
                    raise ControllerUnavailable('unsubmitted claim changed')
                intent = self.db.execute(
                    "SELECT 1 FROM top_pending_intents i JOIN top_fence_intents f ON f.marker=i.marker "
                    "WHERE i.marker=? AND i.uid=? AND i.state='registered' AND f.state='linked'",
                    (marker, uid)).fetchone()
                if intent is None:
                    raise ControllerUnavailable('priority intent changed')
                now = dt.datetime.now(dt.timezone.utc).isoformat()
                changed = self.db.execute(
                    "UPDATE jobs SET state='WAITING_QUOTA',reason_code='priority_wait',"
                    "reason_text='等待优先级控制器就绪',updated_at=? "
                    "WHERE id=? AND owner_uid=? AND state='SUBMITTING' AND scheduler_id IS NULL",
                    (now, marker, uid)).rowcount
                if changed != 1:
                    raise ControllerUnavailable('unsubmitted claim changed')
                self.db.execute('COMMIT')
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
                raise

    def _promotion_reservation_cancel(self, marker, uid):
        """Cancel the monthly hold on the same attested controller instance."""
        from monthly_reservation import ReservationCoordinator, ReservationUncertain
        self._same_controller()
        required_pid = self.bridge.expected_pid
        bridge = self.bridge
        class AttestedCancel:
            def cancel_request(self, uid, request_id, tier, gpu, minutes, *, policy_version=1):
                return bridge.cancel_request(uid, request_id, tier, gpu, minutes, required_pid=required_pid, policy_version=policy_version)
            def reservation_status(self, uid, request_id, *, policy_version=1):
                return bridge.reservation_status(uid, request_id, policy_version=policy_version)
        try:
            ReservationCoordinator(self.db, AttestedCancel()).cancel(marker, uid, ensure_tombstone=True)
        except ReservationUncertain as exc:
            raise ControllerOutcomeUnknown('promotion reservation tombstone needs reconciliation') from exc

    @staticmethod
    def _promotion_members_digest(members):
        canonical = json.dumps([list(row) for row in members], ensure_ascii=False,
                               separators=(',', ':'), allow_nan=False).encode('utf-8')
        return hashlib.sha256(canonical).hexdigest()

    def _promotion_rows_locked(self, uid):
        self.runtime.coordinator.assert_serialized()
        return self.db.execute(
            "SELECT id,owner_name,client_request_id,client_request_hash,effective_job_digest,"
            "effective_job_digest_version,priority_marker_required,selected_tier,state,origin,scheduler_id,cancel_target "
            "FROM jobs WHERE owner_uid=? AND state IN "
            "('WAITING_QUOTA','CANCELLING_RESERVATION','SUBMITTING','PENDING') ORDER BY id", (uid,)).fetchall()

    def _promotion_guard_locked(self, uid, request_id):
        """Permit only the exact fenced batch to bypass the normal policy guard."""
        self.runtime.coordinator.assert_serialized()
        batch = self.db.execute("SELECT state,username,member_count,members_digest FROM top_promotion_batches WHERE request_id=? AND uid=?",
                                (request_id, uid)).fetchone()
        if batch is None or batch[0] != 'fenced':
            return False
        members = self.db.execute('SELECT marker,client_request_id,client_request_hash,effective_digest '
                                  'FROM top_promotion_members WHERE request_id=? ORDER BY marker', (request_id,)).fetchall()
        rows = self._promotion_rows_locked(uid)
        if (len(rows) != len(members) or not rows or batch[2] != len(members) or
                not isinstance(batch[3], str) or not hmac.compare_digest(batch[3], self._promotion_members_digest(members))):
            return False
        for row, member in zip(rows, members):
            marker, name, client_id, client_hash, digest, version, classification, tier, state, origin, sid, target = row
            if (tuple(member) != (marker, client_id, client_hash, digest) or name != batch[1] or
                    version != 1 or classification != 0 or tier not in ('debug','important','normal') or
                    state != 'WAITING_QUOTA' or origin != 'platform' or sid is not None or target is not None or
                    not self._verify_effective_job(uid, marker, digest)):
                return False
        conflict = self.db.execute("SELECT 1 FROM top_fence_intents WHERE uid=? AND state!='closed' "
                                   "UNION ALL SELECT 1 FROM top_pending_intents WHERE uid=? AND state!='cancelled' LIMIT 1",
                                   (uid, uid)).fetchone()
        return conflict is None

    def _verified_promotion_receipt_locked(self, uid, username, generation, version, request_id):
        def verify(policy, status):
            if (policy.get('enabled') is not True or policy.get('version') != version + 1 or
                    policy.get('identity_generation') != generation):
                raise ControllerUnavailable('completed promotion policy is not currently verified')
            batch = self.db.execute('SELECT member_count,members_digest,state FROM top_promotion_batches WHERE request_id=?', (request_id,)).fetchone()
            members = self.db.execute('SELECT marker,client_request_id,client_request_hash,effective_digest '
                                      'FROM top_promotion_members WHERE request_id=? ORDER BY marker', (request_id,)).fetchall()
            if (batch is None or batch[2] != 'complete' or not members or batch[0] != len(members) or
                    not isinstance(batch[1], str) or not hmac.compare_digest(batch[1], self._promotion_members_digest(members))):
                raise ControllerUnavailable('completed promotion members cannot be verified')
            for marker, client_id, client_hash, digest in members:
                job = self.db.execute('SELECT owner_uid,owner_name,client_request_id,client_request_hash,'
                                      'effective_job_digest,state,scheduler_id FROM jobs WHERE id=?', (marker,)).fetchone()
                if job is None or tuple(job[:5]) != (uid, username, client_id, client_hash, digest):
                    raise ControllerUnavailable('completed promotion task identity changed')
                # Historical retries must not re-engage a fence or mutate a
                # marker. The current protocol has no full read-only marker
                # query, so controller-side lifecycle remains unconfirmed.
                if not self._has_marker_locked(marker):
                    raise ControllerUnavailable('completed promotion marker is missing')
            self._check_snapshot(status)
            return {'operation': {'request_id': request_id, 'state': 'pending'}}
        return self.runtime._with_verified_policy_locked(uid, username, verify)

    def _change_or_promote_locked(self, actor, payload):
        """Journal and fence a promotion before changing policy or adopting jobs."""
        self.runtime.coordinator.assert_serialized()
        keys = {'uid','enabled','expected_enabled','expected_version','expected_identity_generation','request_id'}
        if (not isinstance(actor, dict) or actor.get('is_admin') is not True or type(actor.get('uid')) is not int or
                not 0 <= actor['uid'] <= 0xffffffff or not isinstance(actor.get('username'), str) or
                not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', actor['username']) or
                not isinstance(payload, dict) or set(payload) != keys or type(payload['uid']) is not int or
                not 0 <= payload['uid'] <= 0xffffffff or payload['enabled'] is not True or
                payload['expected_enabled'] is not False or type(payload['expected_version']) is not int or
                not 0 <= payload['expected_version'] < 2**53-1 or
                not isinstance(payload['expected_identity_generation'], str) or
                not re.fullmatch(r'[0-9a-f]{32}', payload['expected_identity_generation']) or
                not isinstance(payload['request_id'], str) or not re.fullmatch(r'[A-Za-z0-9-]{8,80}', payload['request_id'])):
            raise ValueError('invalid priority promotion')
        uid, request_id = payload['uid'], payload['request_id']
        user = self.runtime.resolve_user(uid)
        if user.pw_uid != uid:
            raise ControllerUnavailable('promotion account changed')
        username = user.pw_name
        identity = (uid, username, payload['expected_identity_generation'], payload['expected_version'],
                    actor['uid'], actor['username'])
        completed_v2 = self.db.execute('SELECT identity_json FROM top_pending_promotion_v2_completed WHERE request_id=?', (request_id,)).fetchone()
        if completed_v2 is not None:
            if json.loads(completed_v2[0]) != list(identity):
                raise ControllerUnavailable('completed promotion identity conflicts')
            def verify_complete(policy, status):
                if (policy.get('enabled') is not True or policy.get('version') != identity[3] + 1 or
                        policy.get('identity_generation') != identity[2]):
                    raise ControllerUnavailable('completed promotion policy changed')
                return {'operation': {'request_id': request_id, 'state': 'committed'}}
            return self.runtime._with_verified_policy_locked(uid, username, verify_complete)
        pending_v2 = self.db.execute('SELECT uid,identity_json FROM top_pending_promotion_v2 WHERE request_id=?', (request_id,)).fetchone()
        if pending_v2 is not None:
            if pending_v2[0] != uid or json.loads(pending_v2[1]) != list(identity):
                raise ControllerUnavailable('pending promotion identity conflicts')
            # Recovery owns the fenced handoff; retry must not restage members.
            return {'operation': {'request_id': request_id, 'state': 'pending'}}
        if self.db.execute('SELECT 1 FROM top_pending_promotion_v2 LIMIT 1').fetchone():
            raise ControllerUnavailable('another priority promotion is still being recovered')
        existing = self.db.execute('SELECT uid,username,account_generation,expected_version,actor_uid,actor_name,state '
                                   'FROM top_promotion_batches WHERE request_id=?', (request_id,)).fetchone()
        if existing is not None:
            if tuple(existing[:6]) != identity:
                raise ControllerUnavailable('promotion request identity conflicts')
            if existing[6] == 'complete':
                return self._verified_promotion_receipt_locked(uid, username, identity[2], identity[3], request_id)
            return self._resume_promotion_locked(request_id)
        if not self._promotion_rows_locked(uid):
            return self.runtime._change_locked(actor, payload)
        pending_rows = self._promotion_rows_locked(uid)
        if any(row[8] == 'PENDING' for row in pending_rows):
            # This is evidence staging only, not an executable promotion.
            # Revalidation under a controller fence is mandatory before use.
            self._same_controller()
            def verify_pending_promotion_policy(policy, status):
                if (policy.get('enabled') is not False or
                        policy.get('version') != payload['expected_version'] or
                        policy.get('identity_generation') != payload['expected_identity_generation']):
                    from priority_runtime import PriorityConflict
                    raise PriorityConflict('priority policy or account generation changed')
                return dict(status)
            before = self.runtime._with_verified_policy_locked(uid, username, verify_pending_promotion_policy)
            if self.runtime.coordinator.transport.status() != before:
                raise ControllerUnavailable('controller changed before promotion preparation')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                if self.db.execute('SELECT 1 FROM top_pending_promotion_v2 LIMIT 1').fetchone():
                    raise ControllerUnavailable('another priority promotion is still being recovered')
                if [tuple(r) for r in self._promotion_rows_locked(uid)] != [tuple(r) for r in pending_rows]:
                    raise ControllerUnavailable('promotion candidates changed')
                for member in pending_rows:
                    marker, name, client_id, client_hash, digest, version, classification, tier, state, origin, sid, target = member
                    if state != 'PENDING':
                        continue
                    if (name != username or origin != 'platform' or classification != 0 or version != 1 or
                            target is not None or not isinstance(sid, str) or not re.fullmatch(r'[1-9][0-9]{0,9}', sid) or
                            not self._verify_effective_job(uid, marker, digest)):
                        raise ControllerUnavailable('pending promotion identity unavailable')
                    hold = self.db.execute('SELECT * FROM monthly_dispatch_holds WHERE job_id=?', (marker,)).fetchone()
                    proof = self.bridge.pending_binding_receipt(uid, marker, int(sid))
                    remote = self.reservation_status_for_job(uid, marker)
                    if (hold is None or hold['uid'] != uid or hold['state'] != 'BOUND' or hold['binding_version'] != 2 or
                            hold['scheduler_id'] != int(sid) or hold['bind_submit'] != str(proof['submit']) or
                            hold['bind_sluid'] != str(proof['sluid']) or remote.get('state') != 'bound' or
                            remote.get('scheduler_id') != int(sid) or
                            any(remote.get(k) != hold[k] for k in ('uid','tier','gpu','minutes','month'))):
                        raise ControllerUnavailable('pending promotion reservation differs')
                    data = json.dumps({'version': 2, 'uid': uid, 'marker': marker, 'member': list(member),
                                       'submit': proof['submit'], 'sluid': str(proof['sluid']), 'restart': 0,
                                       'reservation': remote}, sort_keys=True, separators=(',', ':'), allow_nan=False)
                    digest_v2 = hashlib.sha256(data.encode('utf-8')).hexdigest()
                    expected = (request_id, marker, uid, int(sid), proof['submit'], str(proof['sluid']), 0, data, 0, digest_v2)
                    old = self.db.execute('SELECT * FROM top_promotion_pending_proofs WHERE request_id=? AND marker=?', (request_id, marker)).fetchone()
                    if old is None:
                        self.db.execute('INSERT INTO top_promotion_pending_proofs VALUES(?,?,?,?,?,?,?,?,?,?)', expected)
                    elif tuple(old) != expected:
                        raise ControllerUnavailable('pending promotion proof conflicts')
                if self.runtime.coordinator.transport.status() != before or not self.runtime.reconciled_for_current_boot():
                    raise ControllerUnavailable('controller changed during pending proof')
                # The complete member set and request identity are durable
                # before any controller fence can be requested.
                identity_json = json.dumps(list(identity), separators=(',', ':'), allow_nan=False)
                proofs = [tuple(r) for r in self.db.execute(
                    'SELECT marker,uid,scheduler_id,submit_time,sluid,restart,reservation_json,classification,proof_digest '
                    'FROM top_promotion_pending_proofs WHERE request_id=? ORDER BY marker', (request_id,))]
                members_json = json.dumps({'version': 2, 'rows': [list(r) for r in pending_rows],
                                           'proofs': proofs}, sort_keys=True, separators=(',', ':'), allow_nan=False)
                batch_digest = hashlib.sha256(members_json.encode('utf-8')).hexdigest()
                expected_batch = (request_id, uid, identity_json, members_json, batch_digest)
                old_batch = self.db.execute('SELECT request_id,uid,identity_json,members_json,digest '
                                            'FROM top_pending_promotion_v2 WHERE request_id=?', (request_id,)).fetchone()
                if old_batch is None:
                    self.db.execute("INSERT INTO top_pending_promotion_v2 VALUES(?,?,?,?,?,'fence_requested')", expected_batch)
                elif tuple(old_batch) != expected_batch:
                    raise ControllerUnavailable('pending promotion batch conflicts')
                self.db.execute('COMMIT')
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
                raise
            self._fence_pending_promotion_v2_locked(request_id)
            self._request_pending_v2_policy_locked(request_id)
            self._adopt_pending_v2_waiting_locked(request_id)
            self._adopt_pending_v2_submitted_locked(request_id)
            self._verify_v2_adopted_locked(request_id)
            # Policy alone is not a completed promotion: marker adoption and
            # audited uncertainty clearing are still required.
            return {'operation': {'request_id': request_id, 'state': 'pending'}}
        def prepare(policy, status):
            if (policy['enabled'] is not False or policy['version'] != payload['expected_version'] or
                    policy['identity_generation'] != payload['expected_identity_generation']):
                raise ControllerUnavailable('promotion policy changed')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                rows = self._promotion_rows_locked(uid)
                if not rows:
                    raise ControllerUnavailable('promotion queue changed')
                if self.db.execute("SELECT 1 FROM top_fence_intents WHERE uid=? AND state!='closed' LIMIT 1", (uid,)).fetchone():
                    raise ControllerUnavailable('existing priority intent prevents promotion')
                snapshot = []
                for row in rows:
                    marker, name, client_id, client_hash, digest, version, classification, tier, state, origin, sid, target = row
                    if (name != username or state != 'WAITING_QUOTA' or origin != 'platform' or sid is not None or
                            target is not None or version != 1 or classification != 0 or
                            tier not in ('debug','important','normal') or not isinstance(marker, str) or
                            not re.fullmatch(r'[0-9a-f]{32}', marker) or not isinstance(client_id, str) or
                            not re.fullmatch(r'[A-Za-z0-9-]{8,80}', client_id) or not isinstance(client_hash, str) or
                            not re.fullmatch(r'[0-9a-f]{64}', client_hash) or not isinstance(digest, str) or
                            not re.fullmatch(r'[0-9a-f]{64}', digest) or not self._verify_effective_job(uid, marker, digest)):
                        # No batch or controller fence has been written yet.
                        # Report the unsupported conversion as a retryable conflict.
                        from priority_runtime import PriorityConflict
                        raise PriorityConflict('submitted or unverifiable task cannot yet be promoted; retry after reconciliation')
                    snapshot.append((marker, client_id, client_hash, digest))
                self.db.execute('INSERT INTO top_promotion_batches VALUES(?,?,?,?,?,?,?,?,?,?)',
                                (request_id, *identity, len(snapshot), self._promotion_members_digest(snapshot), 'prepared'))
                self.db.executemany('INSERT INTO top_promotion_members (request_id,marker,client_request_id,client_request_hash,effective_digest) VALUES(?,?,?,?,?)',
                                    [(request_id, *member) for member in snapshot])
                self.db.execute('COMMIT')
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
                raise
            return self._resume_promotion_locked(request_id)
        return self.runtime._with_verified_policy_locked(uid, username, prepare)

    def _resume_promotion_locked(self, request_id):
        self.runtime.coordinator.assert_serialized()
        row = self.db.execute('SELECT uid,username,account_generation,expected_version,actor_uid,actor_name,state,member_count,members_digest '
                              'FROM top_promotion_batches WHERE request_id=?', (request_id,)).fetchone()
        if row is None:
            raise ControllerUnavailable('promotion batch missing')
        uid, username, generation, version, actor_uid, actor_name, state, member_count, members_digest = row
        if state == 'complete':
            return self._verified_promotion_receipt_locked(uid, username, generation, version, request_id)
        # Never fence or deliver a policy for a future or corrupt batch state.
        # In particular, v2 submitted-job batches require their own recovery.
        if state not in ('prepared', 'fenced', 'policy'):
            raise ControllerUnavailable('promotion batch requires a supported recovery path')
        self._same_controller()
        if not self.runtime.reconciled_for_current_boot():
            raise ControllerUnavailable('priority controller unavailable')
        status = self.runtime.coordinator.transport.status()
        self.bridge.fence_top()
        if 'ledger_seq' in status:
            after = self.runtime.coordinator.transport.status()
            if (not self.runtime.reconciled_for_current_boot() or
                    any(after.get(k) != status.get(k) for k in ('epoch', 'boot', 'watermark')) or
                    after.get('fenced') is not True or type(after.get('ledger_seq')) is not int or
                    after['ledger_seq'] < status['ledger_seq']):
                raise ControllerUnavailable('controller changed during promotion fence')
        else:
            self._check_snapshot(status)
        if state == 'prepared':
            if self.db.execute("UPDATE top_promotion_batches SET state='fenced' WHERE request_id=? AND state='prepared'", (request_id,)).rowcount != 1:
                raise ControllerOutcomeUnknown('promotion fence state changed')
            state = 'fenced'
        if state == 'fenced':
            if not self._promotion_guard_locked(uid, request_id):
                raise ControllerUnavailable('promotion queue changed before policy')
            actor = {'uid': actor_uid, 'username': actor_name, 'is_admin': True}
            payload = {'uid': uid, 'enabled': True, 'expected_enabled': False, 'expected_version': version,
                       'expected_identity_generation': generation, 'request_id': request_id}
            result = self.runtime._change_locked(actor, payload, promotion_request=request_id)
            if result.get('operation', {}).get('state') != 'committed':
                return {'operation': {'request_id': request_id, 'state': 'pending'}}
            if self.db.execute("UPDATE top_promotion_batches SET state='policy' WHERE request_id=? AND state='fenced'", (request_id,)).rowcount != 1:
                raise ControllerOutcomeUnknown('promotion policy state changed')
            state = 'policy'
        if state != 'policy':
            raise ControllerUnavailable('invalid promotion state')
        members = self.db.execute('SELECT marker,client_request_id,client_request_hash,effective_digest '
                                  'FROM top_promotion_members WHERE request_id=? ORDER BY marker', (request_id,)).fetchall()
        if (not members or member_count != len(members) or not isinstance(members_digest, str) or
                not hmac.compare_digest(members_digest, self._promotion_members_digest(members))):
            raise ControllerUnavailable('promotion members missing or changed')
        for marker, client_id, client_hash, digest in members:
            cancellation = self.db.execute('SELECT target,state FROM top_promotion_cancellations WHERE request_id=? AND marker=?',
                                           (request_id, marker)).fetchone()
            if cancellation is None:
                self._adopt_waiting_locked(uid, username, marker)
                continue
            target, cancel_state = cancellation
            if target not in ('CANCELLED', 'EXPIRED') or cancel_state not in ('requested', 'finished'):
                raise ControllerUnavailable('promotion cancellation is invalid')
            job = self.db.execute('SELECT state,scheduler_id,cancel_target FROM jobs WHERE id=? AND owner_uid=?', (marker, uid)).fetchone()
            if job is None or job[1] is not None:
                raise ControllerUnavailable('promotion cancellation task is unavailable')
            now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')
            if job[0] == 'WAITING_QUOTA' and cancel_state == 'requested':
                self._adopt_waiting_locked(uid, username, marker)
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    if self.begin_cancel_in_transaction(marker, target, now, promotion_request=request_id) is not True:
                        raise ControllerUnavailable('promotion cancellation could not be fenced')
                    self.db.execute('COMMIT')
                except BaseException:
                    if self.db.in_transaction:
                        self.db.execute('ROLLBACK')
                    raise
                job = ('CANCELLING_RESERVATION', None, target)
            if job[0] == 'CANCELLING_RESERVATION' and job[2] == target and cancel_state == 'requested':
                if not callable(self.reservation_cancel):
                    raise ControllerUnavailable('promotion reservation cancellation provider unavailable')
                # The provider must use this same authenticated controller and
                # persist CANCEL_EMPTY1/CANCEL1 before marker CANCEL4.
                self._same_controller()
                self.reservation_cancel(marker, uid)
                if not self._finish_cancel_waiting_locked(marker, now):
                    raise ControllerOutcomeUnknown('promotion cancellation marker missing')
                job = (target, None, target)
            if job[0] != target or job[2] != target or not self._has_marker_locked(marker):
                raise ControllerUnavailable('promotion cancellation remains unresolved')
            marker_state = self.db.execute('SELECT state FROM top_pending_intents WHERE marker=?', (marker,)).fetchone()
            if marker_state is None or marker_state[0] != 'cancelled':
                raise ControllerUnavailable('promotion cancellation tombstone missing')
            self.db.execute("UPDATE top_promotion_cancellations SET state='finished' "
                            "WHERE request_id=? AND marker=? AND target=? AND state='requested'", (request_id, marker, target))
        self.db.execute('BEGIN IMMEDIATE')
        try:
            current_members = self.db.execute('SELECT marker,client_request_id,client_request_hash,effective_digest '
                                              'FROM top_promotion_members WHERE request_id=? ORDER BY marker', (request_id,)).fetchall()
            if (len(current_members) != member_count or
                    not hmac.compare_digest(members_digest, self._promotion_members_digest(current_members))):
                raise ControllerUnavailable('promotion members changed')
            if self.db.execute('SELECT 1 FROM top_promotion_cancellations c LEFT JOIN top_promotion_members m '
                               'ON m.request_id=c.request_id AND m.marker=c.marker '
                               'WHERE c.request_id=? AND m.marker IS NULL LIMIT 1', (request_id,)).fetchone():
                raise ControllerUnavailable('promotion cancellation has no member')
            active_rows = self._promotion_rows_locked(uid)
            waiting = set()
            for marker, client_id, client_hash, digest in members:
                job = self.db.execute('SELECT owner_name,client_request_id,client_request_hash,effective_job_digest,'
                                      'effective_job_digest_version,priority_marker_required,selected_tier,state,origin,scheduler_id,cancel_target '
                                      'FROM jobs WHERE id=? AND owner_uid=?', (marker, uid)).fetchone()
                cancellation = self.db.execute('SELECT target,state FROM top_promotion_cancellations WHERE request_id=? AND marker=?',
                                               (request_id, marker)).fetchone()
                if (job is None or tuple(job[:6]) != (username, client_id, client_hash, digest, 1, 1) or
                        job[6] not in ('debug','important','normal') or job[8] != 'platform' or job[9] is not None or
                        not self._verify_effective_job(uid, marker, digest) or not self._has_marker_locked(marker)):
                    raise ControllerUnavailable('promoted task changed')
                marker_state = self.db.execute('SELECT state FROM top_pending_intents WHERE marker=?', (marker,)).fetchone()
                if cancellation is None:
                    if job[7] != 'WAITING_QUOTA' or job[10] is not None or marker_state is None or marker_state[0] != 'registered':
                        raise ControllerUnavailable('promoted task marker is not confirmed')
                    waiting.add(marker)
                elif (cancellation[1] != 'finished' or job[7] != cancellation[0] or job[10] != cancellation[0] or
                      marker_state is None or marker_state[0] != 'cancelled'):
                    raise ControllerUnavailable('promotion cancellation is not confirmed')
            if {row[0] for row in active_rows} != waiting:
                raise ControllerUnavailable('promotion waiting set changed')
            if self.db.execute("UPDATE top_promotion_batches SET state='complete' WHERE request_id=? AND state='policy'", (request_id,)).rowcount != 1:
                raise ControllerOutcomeUnknown('promotion completion changed')
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise
        return {'operation': {'request_id': request_id, 'state': 'committed'}}

    def _has_unresolved_for_uid(self, uid):
        # Called by the policy runtime while holding the same coordinator lock.
        # Existing unstarted ordinary jobs have no top marker. Until migration
        # can fence and register them atomically, changing policy would leave
        # those jobs outside the priority barrier or unable to dispatch.
        return self.db.execute(
            "SELECT 1 FROM top_fence_intents WHERE uid=? AND state!='closed' "
            "UNION ALL SELECT 1 FROM jobs WHERE owner_uid=? AND state IN "
            "('WAITING_QUOTA','CANCELLING_RESERVATION','SUBMITTING','PENDING') "
            "UNION ALL SELECT 1 FROM top_promotion_batches WHERE uid=? AND state!='complete' "
            "UNION ALL SELECT 1 FROM top_pending_promotion_v2 WHERE uid=? LIMIT 1",
            (uid, uid, uid, uid)).fetchone() is not None

    def _same_controller(self):
        transport = self.runtime.coordinator.transport
        peer = transport.bridge
        fields = ('path', 'controller_uid', 'expected_pid')
        expected_pid = getattr(transport, 'expected_pid', None)
        expected_manifest = getattr(transport, 'release_manifest', None)
        if (type(self.bridge.expected_pid) is not int or self.bridge.expected_pid <= 0 or
                any(getattr(peer, name, None) != getattr(self.bridge, name, None) for name in fields) or
                (expected_pid is not None and expected_pid != self.bridge.expected_pid) or
                (expected_manifest is not None and (getattr(peer, 'release_manifest', None) != expected_manifest or
                                                     getattr(self.bridge, 'release_manifest', None) != expected_manifest))):
            raise ControllerUnavailable('policy and marker peers differ')

    def _check_snapshot(self, status):
        if (not self.runtime.reconciled_for_current_boot() or
                self.runtime.coordinator.transport.status() != status):
            raise ControllerUnavailable('controller changed during admission')

    def _refresh_after_controller_write(self, policy, status):
        """Reverify policy after an intentional, attested controller mutation."""
        if 'ledger_seq' not in status:
            self._check_snapshot(status)
            return status
        def verify(current, new_status):
            keys = ('uid', 'username', 'identity_generation', 'enabled', 'version')
            if (any(current.get(k) != policy.get(k) for k in keys) or
                    any(new_status.get(k) != status.get(k) for k in ('epoch', 'boot', 'watermark')) or
                    new_status.get('fenced') is not True or
                    type(new_status.get('ledger_seq')) is not int or
                    new_status['ledger_seq'] < status['ledger_seq']):
                raise ControllerUnavailable('controller policy changed during mutation')
            return new_status
        return self.runtime._with_verified_policy_locked(policy['uid'], policy['username'], verify)

    def _prepare_fence(self, marker, uid, username, generation, request_id, request_hash, effective_digest):
        if (not isinstance(marker, str) or not re.fullmatch(r'[0-9a-f]{32}', marker) or
                type(uid) is not int or not 0 <= uid <= 0xffffffff or
                not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', username) or
                not isinstance(generation, str) or not re.fullmatch(r'[0-9a-f]{32}', generation) or
                not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9-]{8,80}', request_id) or
                not isinstance(request_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', request_hash)):
            raise ValueError('invalid fence intent')
        identity = (marker, uid, username, generation, request_id, request_hash, effective_digest)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            rows = self.db.execute('SELECT marker,uid,username,account_generation,request_id,request_hash,effective_digest,state '
                                   'FROM top_fence_intents WHERE marker=? OR (uid=? AND request_id=?)',
                                   (marker, uid, request_id)).fetchall()
            if rows:
                if len(rows) != 1 or tuple(rows[0][:7]) != identity or rows[0][7] != 'prepared':
                    raise ControllerUnavailable('conflicting fence intent')
            else:
                self.db.execute("INSERT INTO top_fence_intents VALUES(?,?,?,?,?,?,?,'prepared')", identity)
            self.db.execute('COMMIT')
        except BaseException as exc:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise ControllerOutcomeUnknown('fence intent persistence needs reconciliation') from exc

    def enqueue(self, uid, username, marker, request_id, request_hash, effective_digest, insert_job):
        """Fence top jobs, then atomically persist the job and marker intent.

        ``insert_job`` must only write to this database and must not commit,
        call the scheduler, fork, or change priority policy. A raised uncertain
        result after commit leaves the exact intent for recovery. Callers must
        not infer that the controller has cleared its fence.
        """
        if (not callable(insert_job) or not isinstance(effective_digest, str) or
                not re.fullmatch(r'[0-9a-f]{64}', effective_digest)):
            raise ValueError('job insertion callback required')

        def apply(policy, status):
            if self.db.execute("SELECT 1 FROM top_promotion_batches WHERE uid=? AND state!='complete' LIMIT 1", (uid,)).fetchone():
                raise ControllerUnavailable('account promotion is in progress')
            top = policy['enabled'] is True
            existing = self.db.execute('SELECT marker,uid,username,account_generation,request_id,request_hash,effective_digest,state '
                                       'FROM top_fence_intents WHERE marker=? OR (uid=? AND request_id=?)',
                                       (marker, uid, request_id)).fetchall()
            if existing and (len(existing) != 1 or not top or
                             tuple(existing[0]) != (marker, uid, username, policy['identity_generation'],
                                                    request_id, request_hash, effective_digest, 'prepared')):
                raise ControllerUnavailable('previous fence intent conflicts with current policy')
            if top:
                self._same_controller()
                self._prepare_fence(marker, uid, username, policy['identity_generation'], request_id, request_hash, effective_digest)
                self.bridge.fence_top()
                status = self._refresh_after_controller_write(policy, status)
            self.db.execute('BEGIN IMMEDIATE')
            try:
                insert_job(self.db)
                job = self.db.execute('SELECT owner_uid,owner_name,client_request_id,client_request_hash,'
                                      'state,origin,effective_job_digest_version,effective_job_digest '
                                      'FROM jobs WHERE id=?', (marker,)).fetchone()
                if (job is None or tuple(job) != (uid, username, request_id, request_hash,
                                                  'WAITING_QUOTA', 'platform', 1, effective_digest)):
                    raise ControllerUnavailable('queued job does not match validated identity')
                if self.db.execute('UPDATE jobs SET priority_marker_required=? WHERE id=? AND priority_marker_required IS NULL',
                                   (1 if top else 0, marker)).rowcount != 1:
                    raise ControllerUnavailable('priority classification already assigned')
                if top:
                    state = self.intents.prepare(marker, uid, policy['identity_generation'],
                                                 request_id, request_hash, confirmed_top=True)
                    if state != 'pending':
                        raise ControllerUnavailable('marker identity is already in use')
                    if self.db.execute("UPDATE top_fence_intents SET state='linked' WHERE marker=? AND state='prepared'",
                                       (marker,)).rowcount != 1:
                        raise ControllerUnavailable('fence intent changed')
                self.db.execute('COMMIT')
            except BaseException as exc:
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
                raise ControllerOutcomeUnknown('job and marker persistence needs reconciliation') from exc
            if top:
                try:
                    receipt = self.bridge.register_top(marker, uid, policy['identity_generation'],
                                                       request_id, request_hash)
                    identity = (marker, uid, policy['identity_generation'], request_id, request_hash)
                    if receipt != {'registered': True, 'identity': identity}:
                        raise ControllerOutcomeUnknown('marker receipt differs')
                    status = self._refresh_after_controller_write(policy, status)
                    self._confirm_registered(marker, uid, username, policy['identity_generation'],
                                             request_id, request_hash, effective_digest)
                except Exception as exc:
                    raise ControllerOutcomeUnknown('job is queued; marker registration requires recovery') from exc
            return {'marker_required': top, 'marker_registered': top}

        return self.runtime.with_verified_policy(uid, username, apply)

    def _confirm_registered(self, marker, uid, username, generation, request_id, request_hash, digest):
        """Confirm remote registration only while the exact queued job survives."""
        self.runtime.coordinator.assert_serialized()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute(
                'SELECT j.owner_uid,j.owner_name,j.client_request_id,j.client_request_hash,j.state,j.origin,'
                'j.effective_job_digest_version,j.effective_job_digest,j.scheduler_id,j.cancel_target,'
                'i.uid,i.account_generation,i.request_id,i.request_hash,i.state,'
                'f.uid,f.username,f.account_generation,f.request_id,f.request_hash,f.effective_digest,f.state '
                'FROM jobs j JOIN top_pending_intents i ON i.marker=j.id '
                'JOIN top_fence_intents f ON f.marker=j.id WHERE j.id=?', (marker,)).fetchone()
            expected = (uid, username, request_id, request_hash, 'WAITING_QUOTA', 'platform',
                        1, digest, None, None, uid, generation, request_id, request_hash)
            fence = (uid, username, generation, request_id, request_hash, digest, 'linked')
            classification = self.db.execute('SELECT priority_marker_required FROM jobs WHERE id=?', (marker,)).fetchone()
            if (row is None or classification is None or classification[0] != 1 or
                    tuple(row[:14]) != expected or row[14] not in ('pending', 'registered') or
                    tuple(row[15:]) != fence or not self._verify_effective_job(uid, marker, digest)):
                raise ControllerOutcomeUnknown('queued job changed during marker registration')
            if row[14] == 'pending' and not self.intents.mark_registered(marker):
                raise ControllerOutcomeUnknown('marker changed during registration confirmation')
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def _verify_effective_job(self, uid, marker, expected_digest):
        # Read and hash the same fields as the platform's dispatch path. The
        # second check runs inside BEGIN IMMEDIATE to fence SQLite writers.
        if not isinstance(expected_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_digest):
            return False
        row = self.db.execute('SELECT name,requested_gpu,requested_cpu,requested_mem_mb,requested_minutes,'
                              'qos,workdir,env_name,command,output_path,selected_tier,temporary_application_id,policy_version,policy_digest '
                              'FROM jobs WHERE id=? AND owner_uid=?', (marker, uid)).fetchone()
        if row is None:
            return False
        keys = ('name', 'gpu', 'cpu', 'memory_mb', 'time_minutes', 'qos', 'workdir',
                'environment', 'command', 'output_path', 'selected_tier', 'temporary_application_id')
        payload = {'version': 1, 'job_id': marker, 'uid': uid, **dict(zip(keys, row))}
        try:
            actual = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
        except (TypeError, ValueError, OverflowError, UnicodeError):
            return False
        if not hmac.compare_digest(actual, expected_digest) or row['policy_version'] not in (1, 2):
            return False
        if row['policy_version'] == 2:
            expected_policy_digest = hashlib.sha256(('monthly-policy:2:' + expected_digest).encode('ascii')).hexdigest()
            return isinstance(row['policy_digest'], str) and hmac.compare_digest(row['policy_digest'], expected_policy_digest)
        return True

    def adopt_waiting(self, uid, username, marker):
        """Fence and register an existing unstarted platform job after promotion."""
        return self.runtime.with_verified_policy(uid, username,
            lambda policy, status: self._adopt_waiting_with_policy(uid, username, marker, policy, status))

    def _adopt_waiting_locked(self, uid, username, marker):
        """Same operation for a caller that already holds the coordinator lock."""
        self.runtime.coordinator.assert_serialized()
        return self.runtime._with_verified_policy_locked(uid, username,
            lambda policy, status: self._adopt_waiting_with_policy(uid, username, marker, policy, status))

    def _ensure_legacy_waiting_request(self, uid, marker):
        """Persist a namespaced synthetic request only for a verified waiting row."""
        self.runtime.coordinator.assert_serialized()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('CREATE TABLE IF NOT EXISTS legacy_migration_requests '
                            '(marker TEXT PRIMARY KEY, uid INTEGER NOT NULL, request_id TEXT NOT NULL UNIQUE, '
                            'request_hash TEXT NOT NULL, effective_digest TEXT NOT NULL)')
            row = self.db.execute("SELECT client_request_id,client_request_hash,effective_job_digest "
                                  "FROM jobs WHERE id=? AND owner_uid=? AND origin='platform' "
                                  "AND state='WAITING_QUOTA' AND scheduler_id IS NULL AND effective_job_digest_version=1", (marker, uid)).fetchone()
            if row is None or not self._verify_effective_job(uid, marker, row[2]):
                raise ControllerUnavailable('legacy waiting content unavailable')
            request_id, request_hash, digest = row
            if request_id is None and request_hash is None:
                material = ('legacy-migration-v1\0%s\0%d\0%s' % (marker, uid, digest)).encode('ascii')
                value = hashlib.sha256(material).hexdigest()
                request_id, request_hash = 'legacy-' + value, value
                existing = self.db.execute('SELECT marker FROM jobs WHERE owner_uid=? AND client_request_id=? AND id!=?',
                                           (uid, request_id, marker)).fetchone()
                if existing is not None:
                    raise ControllerUnavailable('legacy request identity collision')
                self.db.execute('INSERT INTO legacy_migration_requests VALUES(?,?,?,?,?)',
                                (marker, uid, request_id, request_hash, digest))
                if self.db.execute('UPDATE jobs SET client_request_id=?,client_request_hash=? WHERE id=? '
                                   'AND client_request_id IS NULL AND client_request_hash IS NULL',
                                   (request_id, request_hash, marker)).rowcount != 1:
                    raise ControllerUnavailable('legacy request identity changed')
            elif request_id is None or request_hash is None:
                raise ControllerUnavailable('partial legacy request identity')
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction: self.db.execute('ROLLBACK')
            raise

    def _adopt_waiting_with_policy(self, uid, username, marker, policy, status):
        if policy['enabled'] is not True:
            raise ControllerUnavailable('priority policy is not enabled')
        self._ensure_legacy_waiting_request(uid, marker)
        columns = ('owner_uid,owner_name,client_request_id,client_request_hash,state,origin,'
                   'effective_job_digest_version,effective_job_digest,scheduler_id,cancel_target')
        row = self.db.execute('SELECT ' + columns + ' FROM jobs WHERE id=?', (marker,)).fetchone()
        if row is None:
            raise ControllerUnavailable('waiting task missing')
        owner, name, request_id, request_hash, state, origin, version, digest, sid, target = row
        if (owner != uid or name != username or state != 'WAITING_QUOTA' or origin != 'platform' or
                version != 1 or sid is not None or target is not None or
                not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or
                not self._verify_effective_job(uid, marker, digest)):
            raise ControllerUnavailable('task cannot be safely adopted')
        generation = policy['identity_generation']
        existing = self.db.execute('SELECT state FROM top_fence_intents WHERE marker=?', (marker,)).fetchone()
        if existing is not None and existing[0] == 'linked':
            if not self._recover_marker_locked(marker):
                raise ControllerUnavailable('linked task marker is missing')
            return True
        self._same_controller()
        self._prepare_fence(marker, uid, username, generation, request_id, request_hash, digest)
        self.bridge.fence_top()
        status = self._refresh_after_controller_write(policy, status)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            current = self.db.execute('SELECT ' + columns + ' FROM jobs WHERE id=?', (marker,)).fetchone()
            if (current is None or tuple(current) != tuple(row) or
                    not self._verify_effective_job(uid, marker, digest)):
                raise ControllerUnavailable('task changed while fencing')
            if self.db.execute('UPDATE jobs SET priority_marker_required=1 WHERE id=? AND priority_marker_required=0',
                               (marker,)).rowcount != 1:
                raise ControllerUnavailable('waiting task classification cannot be migrated')
            if self.intents.prepare(marker, uid, generation, request_id, request_hash, confirmed_top=True) != 'pending':
                raise ControllerUnavailable('marker identity already in use')
            if self.db.execute("UPDATE top_fence_intents SET state='linked' WHERE marker=? AND state='prepared'",
                               (marker,)).rowcount != 1:
                raise ControllerUnavailable('fence intent changed')
            self.db.execute('COMMIT')
        except BaseException as exc:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise ControllerOutcomeUnknown('waiting task adoption needs reconciliation') from exc
        try:
            receipt = self.bridge.register_top(marker, uid, generation, request_id, request_hash)
            if receipt != {'registered': True, 'identity': (marker, uid, generation, request_id, request_hash)}:
                raise ControllerOutcomeUnknown('adopted marker receipt differs')
            status = self._refresh_after_controller_write(policy, status)
            self._confirm_registered(marker, uid, username, generation, request_id, request_hash, digest)
        except Exception as exc:
            raise ControllerOutcomeUnknown('adopted marker registration requires recovery') from exc
        return True


    def recover_marker(self, marker):
        """Retry the same persisted marker identity; never clear the fence."""
        with self.runtime.coordinator.serialized():
            return self._recover_marker_locked(marker)

    def pending_marker(self, uid, request_id, request_hash):
        """Reuse a pre-fence identity when a previous response was lost."""
        with self.runtime.coordinator.serialized():
            row = self.db.execute('SELECT marker,request_hash,state FROM top_fence_intents '
                                  'WHERE uid=? AND request_id=?', (uid, request_id)).fetchone()
            if row is None:
                return None
            if row[1] != request_hash or row[2] != 'prepared':
                raise ControllerUnavailable('fence request needs reconciliation')
            return row[0]

    def _recover_orphan_cancellation_locked(self, marker):
        self.runtime.coordinator.assert_serialized()
        entry = self.db.execute('SELECT uid,username,account_generation,request_id,request_hash,effective_digest,target,state '
                                'FROM top_orphan_cancellations WHERE marker=?', (marker,)).fetchone()
        if entry is None:
            raise ControllerUnavailable('prepared cancellation missing')
        uid, username, generation, client_id, client_hash, digest, target, state = entry
        if state == 'finished':
            return True
        if state != 'requested' or target not in ('CANCELLED','EXPIRED'):
            raise ControllerUnavailable('prepared cancellation state is invalid')
        row = self.db.execute('SELECT owner_uid,owner_name,client_request_id,client_request_hash,effective_job_digest,'
                              'state,scheduler_id,cancel_target FROM jobs WHERE id=?', (marker,)).fetchone()
        fence = self.db.execute('SELECT uid,username,account_generation,request_id,request_hash,effective_digest,state '
                                'FROM top_fence_intents WHERE marker=?', (marker,)).fetchone()
        if (row is None or tuple(row[:5]) != (uid, username, client_id, client_hash, digest) or row[6] is not None or
                fence is None or tuple(fence[:6]) != (uid, username, generation, client_id, client_hash, digest) or
                fence[6] not in ('prepared','linked','closed') or not self._verify_effective_job(uid, marker, digest)):
            raise ControllerUnavailable('prepared cancellation task changed')
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')
        if row[5] == 'WAITING_QUOTA':
            self._adopt_waiting_locked(uid, username, marker)
            self.db.execute('BEGIN IMMEDIATE')
            try:
                if self.begin_cancel_in_transaction(marker, target, now) is not True:
                    raise ControllerUnavailable('prepared cancellation could not be fenced')
                self.db.execute('COMMIT')
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
                raise
            row = (uid, username, client_id, client_hash, digest, 'CANCELLING_RESERVATION', None, target)
        if row[5] == 'CANCELLING_RESERVATION' and row[7] == target:
            if not callable(self.reservation_cancel):
                raise ControllerUnavailable('reservation cancellation provider unavailable')
            self._same_controller()
            self.reservation_cancel(marker, uid)
            if not self._finish_cancel_waiting_locked(marker, now):
                raise ControllerOutcomeUnknown('prepared cancellation marker missing')
            row = (uid, username, client_id, client_hash, digest, target, None, target)
        if row[5] != target or row[7] != target or not self._has_marker_locked(marker):
            raise ControllerUnavailable('prepared cancellation remains unresolved')
        result = self.db.execute('SELECT state FROM top_pending_intents WHERE marker=?', (marker,)).fetchone()
        if result is None or result[0] != 'cancelled':
            raise ControllerUnavailable('prepared cancellation marker not confirmed')
        if self.db.execute("UPDATE top_orphan_cancellations SET state='finished' WHERE marker=? AND uid=? AND username=? "
                           "AND account_generation=? AND request_id=? AND request_hash=? AND effective_digest=? "
                           "AND target=? AND state='requested'", (marker, uid, username, generation, client_id, client_hash, digest, target)).rowcount != 1:
            raise ControllerOutcomeUnknown('prepared cancellation state changed')
        return True

    def try_quiescent_generation_cutover(self):
        """Cut over only while every broker intent and local job is quiescent."""
        with self.runtime.coordinator.serialized():
            self.db.execute('BEGIN IMMEDIATE')
            try:
                if not self.runtime.reconciled_for_current_boot():
                    return False
                self._same_controller()
                state = self.bridge._exchange('TOP_GENERATION_STATUS1 0', required_pid=self.bridge.expected_pid)
                if state == 'OK 1':
                    return True
                if state != 'OK 0':
                    raise ControllerUnavailable('generation status unavailable')
                if (self.db.execute("SELECT 1 FROM jobs WHERE origin='platform' AND state IN "
                                    "('SUBMITTING','PENDING','RUNNING','COMPLETING','SUSPENDED','CANCELLING_RESERVATION') LIMIT 1").fetchone() or
                    self.db.execute("SELECT 1 FROM monthly_dispatch_holds WHERE state NOT IN ('CLOSED','CANCELLED') LIMIT 1").fetchone() or
                    self.db.execute("SELECT 1 FROM top_fence_intents WHERE state!='closed' LIMIT 1").fetchone() or
                    self.db.execute("SELECT 1 FROM top_pending_intents WHERE state!='cancelled' LIMIT 1").fetchone() or
                    self.runtime.store.outbox.pending() is not None or self.runtime.store.pending_confirmations()):
                    return False
                self._same_controller()
                self.bridge.fence_top()
                return self.bridge._exchange('TOP_GENERATION_CUTOVER1 0', mutation=True,
                                             required_pid=self.bridge.expected_pid) == 'OK CUTOVER1'
            finally:
                self.db.execute('ROLLBACK')

    def _fence_pending_promotion_v2_locked(self, request_id):
        self.runtime.coordinator.assert_serialized()
        row = self.db.execute('SELECT members_json,digest FROM top_pending_promotion_v2 WHERE request_id=?', (request_id,)).fetchone()
        if row is None or not hmac.compare_digest(hashlib.sha256(row[0].encode('utf-8')).hexdigest(), row[1]):
            raise ControllerUnavailable('pending promotion journal invalid')
        self._same_controller()
        before = self.runtime.coordinator.transport.status()
        self.bridge.fence_top()
        after = self.runtime.coordinator.transport.status()
        if (not self.runtime.reconciled_for_current_boot() or not after.get('fenced') or
                any(before.get(k) != after.get(k) for k in ('epoch', 'boot', 'watermark'))):
            raise ControllerUnavailable('pending promotion fence not confirmed')
        snapshot = json.loads(row[0])
        if not isinstance(snapshot, dict) or snapshot.get('version') != 2 or not isinstance(snapshot.get('proofs'), list):
            raise ControllerUnavailable('pending promotion snapshot invalid')
        rows = snapshot.get('rows')
        if not isinstance(rows, list) or not rows or any(not isinstance(x, list) or len(x) != 12 for x in rows):
            raise ControllerUnavailable('promotion membership invalid')
        members = {x[0]: x for x in rows}
        proofs = {x[0]: x for x in snapshot['proofs'] if isinstance(x, list) and len(x) == 9}
        if (len(members) != len(rows) or len(proofs) != len(snapshot['proofs']) or
                set(proofs) != {x[0] for x in rows if x[8] == 'PENDING'} or
                any(x[8] not in ('PENDING', 'WAITING_QUOTA') for x in rows)):
            raise ControllerUnavailable('promotion proof coverage invalid')
        batch_uid = self.db.execute('SELECT uid FROM top_pending_promotion_v2 WHERE request_id=?', (request_id,)).fetchone()
        if batch_uid is None:
            raise ControllerUnavailable('promotion batch missing')
        def read_members():
            result = {r[0]: list(r) for r in self._promotion_rows_locked(batch_uid[0])}
            for marker, member in members.items():
                if marker not in result and member[8] == 'PENDING':
                    running = self.db.execute(
                        "SELECT id,owner_name,client_request_id,client_request_hash,effective_job_digest,"
                        "effective_job_digest_version,priority_marker_required,selected_tier,state,origin,scheduler_id,cancel_target "
                        "FROM jobs WHERE id=? AND owner_uid=? AND state IN ('RUNNING','COMPLETED','CANCELLED','FAILED')", (marker, batch_uid[0])).fetchone()
                    if running is not None:
                        result[marker] = list(running)
            return result
        current_rows = read_members()
        if set(current_rows) != set(members):
            raise ControllerUnavailable('promotion membership changed')
        waiting = []
        for marker, member in members.items():
            current = current_rows[marker]
            if member[8] == 'WAITING_QUOTA':
                adopted = (current[:6] == member[:6] and current[6] == 1 and
                           current[7:] == member[7:] and self._has_marker_locked(marker))
                if (current != member and not adopted) or member[10] is not None or member[11] is not None:
                    raise ControllerUnavailable('waiting promotion member changed')
                if self.db.execute("SELECT 1 FROM monthly_dispatch_holds WHERE job_id=? AND state NOT IN ('CLOSED','CANCELLED','DEFERRED')", (marker,)).fetchone():
                    raise ControllerUnavailable('waiting reservation requires reconciliation')
                waiting.append((request_id, marker, json.dumps(member, separators=(',', ':'), allow_nan=False)))
            else:
                adopted = (current[:6] == member[:6] and current[6] == 1 and current[7:8] == member[7:8] and
                           self._has_marker_locked(marker))
                if ((current[:8] != member[:8] and not adopted) or current[9:] != member[9:] or
                        current[8] not in ('PENDING', 'RUNNING', 'COMPLETED', 'CANCELLED', 'FAILED')):
                    raise ControllerUnavailable('submitted promotion member changed')
            if not self._verify_effective_job(batch_uid[0], marker, member[4]):
                raise ControllerUnavailable('promotion effective identity changed')
        observed = []
        observed_holds = {}
        for proof in snapshot['proofs']:
            if not isinstance(proof, list) or len(proof) != 9:
                raise ControllerUnavailable('pending promotion proof invalid')
            marker, uid, sid, submit, sluid, restart, data, classification, digest = proof
            if (uid != batch_uid[0] or str(sid) != members[marker][10] or restart != 0 or classification != 0 or not isinstance(data, str) or
                    not hmac.compare_digest(hashlib.sha256(data.encode('utf-8')).hexdigest(), digest)):
                raise ControllerUnavailable('pending promotion proof differs')
            try:
                staged = json.loads(data)
            except (ValueError, TypeError) as exc:
                raise ControllerUnavailable('pending promotion proof malformed') from exc
            if (not isinstance(staged, dict) or staged.get('version') != 2 or staged.get('uid') != uid or
                    staged.get('marker') != marker or staged.get('member') != members[marker] or
                    staged.get('submit') != submit or staged.get('sluid') != sluid or
                    staged.get('restart') != restart or not isinstance(staged.get('reservation'), dict)):
                raise ControllerUnavailable('pending promotion proof contents differ')
            if current_rows[marker][8] in ('COMPLETED', 'CANCELLED', 'FAILED'):
                terminal = self._verify_v2_finished_locked(request_id, marker, uid, sid)
                if terminal[:4] != (uid, sid, submit, sluid):
                    raise ControllerUnavailable('terminal promotion identity changed')
                observed.append((request_id, marker, uid, sid, submit, sluid, restart, str(terminal[4]), 'running' if terminal[4] else 'pending'))
                continue
            hold = self.db.execute('SELECT * FROM monthly_dispatch_holds WHERE job_id=?', (marker,)).fetchone()
            if (hold is None or hold['uid'] != uid or hold['scheduler_id'] != sid or hold['state'] != 'BOUND' or
                    hold['binding_version'] != 2 or hold['bind_submit'] != str(submit) or hold['bind_sluid'] != sluid or
                    any(staged['reservation'].get(k) != hold[k] for k in ('uid', 'tier', 'gpu', 'minutes', 'month'))):
                raise ControllerUnavailable('pending promotion hold changed')
            observed_holds[marker] = dict(hold)
            try:
                current = self.bridge.pending_binding_receipt(uid, marker, sid)
                state, generation = 'pending', 0
            except ControllerUnavailable:
                classified = self.db.execute('SELECT priority_marker_required FROM jobs WHERE id=?', (marker,)).fetchone()
                current = (self.bridge.top_running_receipt(uid, marker, sid) if classified is not None and classified[0] == 1
                           else self.bridge.ordinary_running_receipt(uid, marker, sid))
                state, generation = 'running', current['generation']
            if current['submit'] != submit or str(current['sluid']) != sluid:
                raise ControllerUnavailable('pending promotion allocation identity differs')
            reservation = self.reservation_status_for_job(uid, marker)
            if (reservation.get('scheduler_id') != sid or
                    reservation.get('state') != ('bound' if state == 'pending' else 'active') or
                    (state == 'running' and reservation.get('generation') != generation) or
                    any(reservation.get(k) != hold[k] for k in ('uid', 'tier', 'gpu', 'minutes', 'month'))):
                raise ControllerUnavailable('pending promotion current reservation differs')
            observed.append((request_id, marker, uid, sid, submit, sluid, restart, str(generation), state))
        if self.runtime.coordinator.transport.status() != after or not self.runtime.reconciled_for_current_boot():
            raise ControllerUnavailable('controller changed during pending promotion audit')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if read_members() != current_rows:
                raise ControllerUnavailable('promotion membership changed during audit')
            for marker, expected_hold in observed_holds.items():
                hold = self.db.execute('SELECT * FROM monthly_dispatch_holds WHERE job_id=?', (marker,)).fetchone()
                if hold is None or dict(hold) != expected_hold:
                    raise ControllerUnavailable('pending promotion hold changed during audit')
            for item in waiting:
                old = self.db.execute('SELECT request_id,marker,member_json FROM top_pending_promotion_v2_waiting WHERE request_id=? AND marker=?', item[:2]).fetchone()
                if old is None:
                    self.db.execute('INSERT INTO top_pending_promotion_v2_waiting VALUES(?,?,?)', item)
                elif tuple(old) != item:
                    raise ControllerUnavailable('waiting promotion observation conflicts')
            for item in observed:
                old = self.db.execute('SELECT * FROM top_pending_promotion_v2_observed WHERE request_id=? AND marker=?', item[:2]).fetchone()
                if old is None:
                    self.db.execute('INSERT INTO top_pending_promotion_v2_observed VALUES(?,?,?,?,?,?,?,?,?)', item)
                elif tuple(old) != item:
                    if tuple(old[:7]) != item[:7] or tuple(old[7:]) != ('0', 'pending') or item[8] != 'running':
                        raise ControllerUnavailable('pending promotion observation conflicts')
                    self.db.execute("UPDATE top_pending_promotion_v2_observed SET generation=?,state='running' WHERE request_id=? AND marker=? AND state='pending' AND generation='0'", (item[7], *item[:2]))
            seen_submitted = {r[0] for r in self.db.execute('SELECT marker FROM top_pending_promotion_v2_observed WHERE request_id=?', (request_id,))}
            seen_waiting = {r[0] for r in self.db.execute('SELECT marker FROM top_pending_promotion_v2_waiting WHERE request_id=?', (request_id,))}
            if seen_submitted != set(proofs) or seen_waiting != {x[0] for x in rows if x[8] == 'WAITING_QUOTA'}:
                raise ControllerUnavailable('promotion observation coverage differs')
            self.db.execute("UPDATE top_pending_promotion_v2 SET state='quarantined' WHERE request_id=?", (request_id,))
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def _request_pending_v2_policy_locked(self, request_id):
        """Deliver the same ordered request after a completed fenced audit."""
        self.runtime.coordinator.assert_serialized()
        row = self.db.execute('SELECT uid,identity_json,state FROM top_pending_promotion_v2 WHERE request_id=?', (request_id,)).fetchone()
        if row is None or row[2] != 'quarantined':
            raise ControllerUnavailable('pending promotion is not audited')
        try:
            identity = json.loads(row[1])
            uid, username, generation, version, actor_uid, actor_name = identity
        except (ValueError, TypeError) as exc:
            raise ControllerUnavailable('pending promotion policy identity invalid') from exc
        if uid != row[0] or type(version) is not int or not 0 <= version < 2**53-1:
            raise ControllerUnavailable('pending promotion policy identity differs')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute('SELECT identity_json FROM top_pending_promotion_v2_policy WHERE request_id=?', (request_id,)).fetchone()
            if old is None:
                self.db.execute("INSERT INTO top_pending_promotion_v2_policy VALUES(?,?,'policy_requested')", (request_id, row[1]))
            elif old[0] != row[1]:
                raise ControllerUnavailable('pending promotion policy journal differs')
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction: self.db.execute('ROLLBACK')
            raise
        actor = {'uid': actor_uid, 'username': actor_name, 'is_admin': True}
        self.runtime.store.request_change(actor=actor, uid=uid, username=username, enabled=True,
            expected_enabled=False, expected_version=version, request_id=request_id, identity_generation=generation)
        self.runtime.coordinator._deliver_locked()
        def verify(policy, status):
            if (policy.get('enabled') is not True or policy.get('version') != version + 1 or
                    policy.get('identity_generation') != generation or not status.get('fenced')):
                raise ControllerUnavailable('pending promotion policy not verified')
            return True
        self.runtime._with_verified_policy_locked(uid, username, verify)
        self.db.execute("UPDATE top_pending_promotion_v2_policy SET state='policy_committed' WHERE request_id=? AND identity_json=? AND state='policy_requested'", (request_id, row[1]))
        phase = self.db.execute('SELECT identity_json,state FROM top_pending_promotion_v2_policy WHERE request_id=?', (request_id,)).fetchone()
        if phase is None or tuple(phase) != (row[1], 'policy_committed'):
            raise ControllerUnavailable('pending promotion policy confirmation changed')

    def _adopt_pending_v2_waiting_locked(self, request_id):
        """Adopt audited waiting members without clearing the v2 fence."""
        self.runtime.coordinator.assert_serialized()
        batch = self.db.execute('SELECT uid,identity_json,members_json,digest,state FROM top_pending_promotion_v2 WHERE request_id=?', (request_id,)).fetchone()
        phase = self.db.execute('SELECT identity_json,state FROM top_pending_promotion_v2_policy WHERE request_id=?', (request_id,)).fetchone()
        if (batch is None or batch[4] != 'quarantined' or phase is None or
                tuple(phase) != (batch[1], 'policy_committed') or
                not hmac.compare_digest(hashlib.sha256(batch[2].encode('utf-8')).hexdigest(), batch[3])):
            raise ControllerUnavailable('pending promotion policy or batch unavailable')
        identity = json.loads(batch[1])
        snapshot = json.loads(batch[2])
        if not isinstance(identity, list) or len(identity) != 6 or identity[0] != batch[0] or snapshot.get('version') != 2:
            raise ControllerUnavailable('pending promotion identity invalid')
        rows = snapshot.get('rows')
        if not isinstance(rows, list) or not rows:
            raise ControllerUnavailable('pending promotion membership unavailable')
        for member in rows:
            if not isinstance(member, list) or len(member) != 12:
                raise ControllerUnavailable('pending promotion member invalid')
            if member[8] != 'WAITING_QUOTA':
                continue
            marker = member[0]
            expected = json.dumps(member, separators=(',', ':'), allow_nan=False)
            observation = self.db.execute('SELECT member_json FROM top_pending_promotion_v2_waiting WHERE request_id=? AND marker=?', (request_id, marker)).fetchone()
            if observation is None or observation[0] != expected:
                raise ControllerUnavailable('waiting member was not audited')
            self._adopt_waiting_locked(batch[0], identity[1], marker)
            if not self._has_marker_locked(marker):
                raise ControllerUnavailable('waiting marker adoption unconfirmed')

    def _adopt_pending_v2_submitted_locked(self, request_id):
        """Adopt exact audited pending tasks; unresolved outcomes stay fenced."""
        self.runtime.coordinator.assert_serialized()
        batch = self.db.execute('SELECT uid,identity_json,members_json,digest,state FROM top_pending_promotion_v2 WHERE request_id=?', (request_id,)).fetchone()
        phase = self.db.execute('SELECT identity_json,state FROM top_pending_promotion_v2_policy WHERE request_id=?', (request_id,)).fetchone()
        if (batch is None or batch[4] != 'quarantined' or phase is None or tuple(phase) != (batch[1], 'policy_committed') or
                not hmac.compare_digest(hashlib.sha256(batch[2].encode('utf-8')).hexdigest(), batch[3])):
            raise ControllerUnavailable('pending promotion batch not verified')
        identity, snapshot = json.loads(batch[1]), json.loads(batch[2])
        if not isinstance(identity, list) or len(identity) != 6 or identity[0] != batch[0] or snapshot.get('version') != 2:
            raise ControllerUnavailable('pending promotion identity invalid')
        uid, username, account = identity[:3]
        for member in snapshot.get('rows', []):
            if not isinstance(member, list) or len(member) != 12:
                raise ControllerUnavailable('pending promotion member invalid')
            if member[8] != 'PENDING':
                continue
            marker, name, client_id, client_hash, digest, version, classification, tier, _, origin, sid, target = member
            observed = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,restart,generation,state FROM top_pending_promotion_v2_observed WHERE request_id=? AND marker=?', (request_id, marker)).fetchone()
            if observed is None or observed[0] != uid or str(observed[1]) != sid or observed[4] != 0:
                raise ControllerUnavailable('submitted promotion observation unavailable')
            terminal_job = self.db.execute('SELECT state FROM jobs WHERE id=?', (marker,)).fetchone()
            if terminal_job is not None and terminal_job[0] in ('COMPLETED', 'CANCELLED', 'FAILED'):
                self._verify_v2_finished_locked(request_id, marker, uid, sid)
                continue
            if observed[6] == 'running':
                continue
            if observed[6] != 'pending' or observed[5] != '0' or name != username or version != 1 or classification != 0 or origin != 'platform' or target is not None:
                raise ControllerUnavailable('submitted promotion observation invalid')
            job_id, submit, sluid = observed[1:4]
            self.db.execute('BEGIN IMMEDIATE')
            try:
                job = self.db.execute('SELECT owner_uid,owner_name,client_request_id,client_request_hash,effective_job_digest,state,scheduler_id,priority_marker_required FROM jobs WHERE id=?', (marker,)).fetchone()
                if (job is None or tuple(job[:7]) != (uid, username, client_id, client_hash, digest, 'PENDING', sid) or
                        job[7] not in (0, 1) or not self._verify_effective_job(uid, marker, digest)):
                    raise ControllerUnavailable('submitted promotion job changed')
                fence = (marker, uid, username, account, client_id, client_hash, digest, 'linked')
                old = self.db.execute('SELECT marker,uid,username,account_generation,request_id,request_hash,effective_digest,state FROM top_fence_intents WHERE marker=?', (marker,)).fetchone()
                if old is None:
                    self.db.execute('INSERT INTO top_fence_intents VALUES(?,?,?,?,?,?,?,?)', fence)
                elif tuple(old) != fence:
                    raise ControllerUnavailable('submitted promotion fence conflicts')
                state = self.intents.prepare(marker, uid, account, client_id, client_hash, confirmed_top=True)
                if state not in ('pending', 'registered'):
                    raise ControllerUnavailable('submitted promotion intent conflicts')
                self.db.execute('COMMIT')
            except BaseException:
                if self.db.in_transaction: self.db.execute('ROLLBACK')
                raise
            registration = self.bridge.register_top(marker, uid, account, client_id, client_hash)
            if registration != {'registered': True, 'identity': (marker, uid, account, client_id, client_hash)}:
                raise ControllerUnavailable('submitted promotion registration differs')
            bound = self.bridge.bind_top(marker, uid, account, client_id, client_hash, job_id)
            pending = self.bridge.top_pending_receipt(uid, marker, job_id)
            if (bound['submit'] != submit or str(bound['sluid']) != sluid or pending['submit'] != submit or str(pending['sluid']) != sluid):
                raise ControllerUnavailable('submitted promotion binding differs')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                job = self.db.execute('SELECT owner_uid,owner_name,client_request_id,client_request_hash,effective_job_digest,state,scheduler_id,priority_marker_required FROM jobs WHERE id=?', (marker,)).fetchone()
                if (job is None or tuple(job[:7]) != (uid, username, client_id, client_hash, digest, 'PENDING', sid) or
                        job[7] not in (0, 1) or not self._verify_effective_job(uid, marker, digest)):
                    raise ControllerUnavailable('submitted promotion job changed before commit')
                binding = (marker, uid, job_id, submit, sluid, 0, 0)
                old = self.db.execute('SELECT marker,uid,scheduler_id,submit_time,sluid,restart,generation FROM top_marker_bindings WHERE marker=?', (marker,)).fetchone()
                if old is None: self.db.execute('INSERT INTO top_marker_bindings VALUES(?,?,?,?,?,?,?)', binding)
                elif tuple(old) != binding: raise ControllerUnavailable('submitted promotion binding conflicts')
                self.db.execute("UPDATE top_pending_intents SET state='registered' WHERE marker=? AND state='pending'", (marker,))
                if self.db.execute('UPDATE jobs SET priority_marker_required=1 WHERE id=? AND priority_marker_required IN (0,1)', (marker,)).rowcount != 1:
                    raise ControllerUnavailable('submitted promotion classification changed')
                if not self._has_marker_locked(marker): raise ControllerUnavailable('submitted promotion marker unconfirmed')
                self.db.execute('COMMIT')
            except BaseException:
                if self.db.in_transaction: self.db.execute('ROLLBACK')
                raise

    def _verify_v2_adopted_locked(self, request_id):
        """Record full adoption coverage; this does not authorize a clear."""
        self.runtime.coordinator.assert_serialized()
        row = self.db.execute('SELECT uid,members_json,digest,state FROM top_pending_promotion_v2 WHERE request_id=?', (request_id,)).fetchone()
        if row is None or row[3] != 'quarantined' or not hmac.compare_digest(hashlib.sha256(row[1].encode('utf-8')).hexdigest(), row[2]):
            raise ControllerUnavailable('promotion batch unavailable')
        snapshot = json.loads(row[1])
        members = snapshot.get('rows') if isinstance(snapshot, dict) and snapshot.get('version') == 2 else None
        if not isinstance(members, list) or not members or any(not isinstance(m, list) or len(m) != 12 for m in members):
            raise ControllerUnavailable('promotion membership invalid')
        if len({m[0] for m in members}) != len(members):
            raise ControllerUnavailable('promotion membership duplicated')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            phase = self.db.execute('SELECT state FROM top_pending_promotion_v2_policy WHERE request_id=?', (request_id,)).fetchone()
            if phase is None or phase[0] != 'policy_committed':
                raise ControllerUnavailable('promotion policy unconfirmed')
            for member in members:
                marker, _, _, _, digest, _, _, _, state, _, sid, _ = member
                if not self._verify_effective_job(row[0], marker, digest):
                    raise ControllerUnavailable('promotion member identity changed')
                if state == 'WAITING_QUOTA':
                    if not self._has_marker_locked(marker):
                        raise ControllerUnavailable('waiting member not adopted')
                elif state == 'PENDING':
                    proof = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,restart,generation,state FROM top_pending_promotion_v2_observed WHERE request_id=? AND marker=?', (request_id, marker)).fetchone()
                    if proof is None or proof[0] != row[0] or str(proof[1]) != sid:
                        raise ControllerUnavailable('submitted member observation missing')
                    terminal_job = self.db.execute('SELECT state FROM jobs WHERE id=?', (marker,)).fetchone()
                    if terminal_job is not None and terminal_job[0] in ('COMPLETED', 'CANCELLED', 'FAILED'):
                        terminal_identity = self._verify_v2_finished_locked(request_id, marker, row[0], sid)
                        if tuple(proof[:4]) != terminal_identity[:4]:
                            raise ControllerUnavailable('terminal member observation differs')
                    elif proof[6] == 'pending':
                        binding = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,restart,generation FROM top_marker_bindings WHERE marker=?', (marker,)).fetchone()
                        if binding is None or tuple(binding) != tuple(proof[:5]) + (0,) or not self._has_marker_locked(marker):
                            raise ControllerUnavailable('submitted member not adopted')
                    elif proof[6] != 'running' or not proof[5] or proof[5] == '0':
                        raise ControllerUnavailable('running member unverified')
                else:
                    raise ControllerUnavailable('unsupported promotion lifecycle')
            old = self.db.execute('SELECT members_digest,state FROM top_pending_promotion_v2_adopted WHERE request_id=?', (request_id,)).fetchone()
            if old is None:
                self.db.execute("INSERT INTO top_pending_promotion_v2_adopted VALUES(?,?,'adopted')", (request_id, row[2]))
            elif tuple(old) != (row[2], 'adopted'):
                raise ControllerUnavailable('promotion adopted record conflicts')
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction: self.db.execute('ROLLBACK')
            raise

    def _complete_pending_v2_locked(self, request_id):
        """Clear only a fully audited batch; unknown outcomes are re-fenced."""
        self.runtime.coordinator.assert_serialized()
        # This transaction commits before any clear RPC: a lost response or
        # process death cannot erase evidence of a possibly applied clear.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            candidate = self.db.execute('SELECT digest FROM top_pending_promotion_v2 WHERE request_id=?', (request_id,)).fetchone()
            if candidate is None:
                raise ControllerUnavailable('v2 clear batch missing')
            old_attempt = self.db.execute('SELECT members_digest,state FROM top_pending_promotion_v2_clear_attempt WHERE request_id=?', (request_id,)).fetchone()
            if old_attempt is None:
                self.db.execute("INSERT INTO top_pending_promotion_v2_clear_attempt VALUES(?,?,'outcome_unknown')", (request_id, candidate[0]))
            elif tuple(old_attempt) != (candidate[0], 'outcome_unknown'):
                raise ControllerUnavailable('v2 clear attempt conflicts')
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction: self.db.execute('ROLLBACK')
            raise
        self.db.execute('BEGIN IMMEDIATE')
        try:
            batches = self.db.execute('SELECT request_id,uid,identity_json,members_json,digest FROM top_pending_promotion_v2').fetchall()
            if len(batches) != 1 or batches[0][0] != request_id:
                raise ControllerUnavailable('v2 completion requires exactly one batch')
            batch = tuple(batches[0])
            payload, digest = self._fence_snapshot_in_transaction(include_adopted_v2=True)
            self._same_controller()
            before = self.runtime.coordinator.transport.status()
            if any(before.get(k) != payload[k] for k in ('epoch','boot','watermark','ledger_seq')):
                raise ControllerUnavailable('v2 controller changed before clear')
            if not hmac.compare_digest(self.bridge.top_snapshot_digest(payload['epoch'], payload['boot'], payload['watermark']), digest):
                raise ControllerUnavailable('v2 controller snapshot differs')
            self.bridge.clear_top_uncertainty(payload['epoch'], payload['boot'], payload['watermark'], payload['ledger_seq'], digest)
            if not hmac.compare_digest(self.bridge.top_snapshot_digest(payload['epoch'], payload['boot'], payload['watermark']), digest):
                raise ControllerOutcomeUnknown('v2 snapshot changed after clear')
            receipt = batch + (payload['epoch'], payload['boot'], str(payload['watermark']), str(payload['ledger_seq']), digest)
            old = self.db.execute('SELECT * FROM top_pending_promotion_v2_completed WHERE request_id=?', (request_id,)).fetchone()
            if old is None:
                self.db.execute('INSERT INTO top_pending_promotion_v2_completed VALUES(?,?,?,?,?,?,?,?,?,?)', receipt)
            elif tuple(old) != receipt:
                raise ControllerUnavailable('v2 completion receipt conflicts')
            if self.db.execute('DELETE FROM top_pending_promotion_v2 WHERE request_id=? AND digest=?', (request_id, batch[4])).rowcount != 1:
                raise ControllerUnavailable('v2 batch changed during completion')
            if self.db.execute('DELETE FROM top_pending_promotion_v2_clear_attempt WHERE request_id=? AND members_digest=?', (request_id, batch[4])).rowcount != 1:
                raise ControllerUnavailable('v2 clear attempt changed')
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction: self.db.execute('ROLLBACK')
            try: self.bridge.fence_top()
            except Exception: pass
            raise

    def _recover_v2_cancelled_locked(self, request_id, marker, uid, sid):
        """Consume only an existing exact, never-started tombstone."""
        self.runtime.coordinator.assert_serialized()
        binding = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,restart,generation FROM top_marker_bindings WHERE marker=?', (marker,)).fetchone()
        intent = self.db.execute('SELECT uid,account_generation,request_id,request_hash,state FROM top_pending_intents WHERE marker=?', (marker,)).fetchone()
        journal = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,state FROM top_submitted_cancel_intents WHERE marker=?', (marker,)).fetchone()
        if (binding is None or binding[0] != uid or binding[1] != sid or tuple(binding[4:]) != (0,0) or
                intent is None or intent[0] != uid or intent[4] not in ('registered','cancelled') or
                journal is None or tuple(journal[:4]) != (uid,sid,binding[2],binding[3]) or
                journal[4] not in ('requested','reservation_cancelled')):
            raise ControllerUnavailable('cancellation journal identity unavailable')
        self._same_controller()
        before = self.runtime.coordinator.transport.status()
        self.bridge.cancelled_top_receipt(marker, uid, intent[1], intent[2], intent[3], sid, binding[2], int(binding[3]))
        remote = self.reservation_status_for_job(uid, marker)
        if (remote.get('state') != 'cancelled_bound' or remote.get('scheduler_id') != sid or
                remote.get('generation') is not None or self.runtime.coordinator.transport.status() != before):
            raise ControllerUnavailable('cancellation tombstone differs')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if (tuple(self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,restart,generation FROM top_marker_bindings WHERE marker=?', (marker,)).fetchone()) != tuple(binding) or
                    tuple(self.db.execute('SELECT uid,account_generation,request_id,request_hash,state FROM top_pending_intents WHERE marker=?', (marker,)).fetchone()) != tuple(intent) or
                    tuple(self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,state FROM top_submitted_cancel_intents WHERE marker=?', (marker,)).fetchone()) != tuple(journal)):
                raise ControllerUnavailable('cancellation identity changed')
            hold = self.db.execute('SELECT uid,scheduler_id,state,binding_version,bind_submit,bind_sluid FROM monthly_dispatch_holds WHERE job_id=?', (marker,)).fetchone()
            job = self.db.execute('SELECT owner_uid,scheduler_id,state,priority_marker_required,effective_job_digest FROM jobs WHERE id=?', (marker,)).fetchone()
            fence = self.db.execute('SELECT uid,account_generation,request_id,request_hash,state FROM top_fence_intents WHERE marker=?', (marker,)).fetchone()
            if (fence is None or tuple(fence[:4]) != (uid,intent[1],intent[2],intent[3]) or
                    (intent[4], fence[4]) not in (('registered','linked'),('cancelled','closed')) or
                    self.db.execute('SELECT 1 FROM top_started_receipts WHERE marker=?', (marker,)).fetchone()):
                raise ControllerUnavailable('cancelled priority fence differs')
            if (hold is None or tuple(hold) not in ((uid,sid,'BOUND',2,str(binding[2]),binding[3]), (uid,sid,'CLOSED',2,str(binding[2]),binding[3])) or
                    job is None or job[0] != uid or str(job[1]) != str(sid) or job[2] not in ('PENDING','CANCELLED') or job[3] != 1 or
                    not self._verify_effective_job(uid, marker, job[4])):
                raise ControllerUnavailable('cancelled job identity differs')
            item = (request_id, marker, uid, sid, binding[2], binding[3])
            old = self.db.execute('SELECT * FROM top_pending_promotion_v2_cancelled WHERE request_id=? AND marker=?', (request_id, marker)).fetchone()
            if old is None: self.db.execute('INSERT INTO top_pending_promotion_v2_cancelled VALUES(?,?,?,?,?,?)', item)
            elif tuple(old) != item: raise ControllerUnavailable('cancelled observation conflicts')
            self.db.execute("UPDATE monthly_dispatch_holds SET state='CLOSED' WHERE job_id=? AND state='BOUND'", (marker,))
            self.db.execute("UPDATE top_submitted_cancel_intents SET state='reservation_cancelled' WHERE marker=? AND state='requested'", (marker,))
            self.db.execute("UPDATE top_pending_intents SET state='cancelled' WHERE marker=? AND state='registered'", (marker,))
            self.db.execute("UPDATE top_fence_intents SET state='closed' WHERE marker=? AND state='linked'", (marker,))
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            self.db.execute("UPDATE jobs SET state='CANCELLED',reason_code='cancelled_by_user',reason_text='已取消',updated_at=?,finished_at=COALESCE(finished_at,?) WHERE id=? AND state='PENDING'", (now, now, marker))
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction: self.db.execute('ROLLBACK')
            raise

    def recover_pending(self):
        """Retry ordered policies, then known job and promotion identities."""
        with self.runtime.coordinator.serialized():
            pending_v2 = [r[0] for r in self.db.execute('SELECT request_id FROM top_pending_promotion_v2 ORDER BY request_id')]
            for request_id in pending_v2:
                # Fence on every recovery, not only after a clear attempt:
                # cancellation can occur after adoption and before clear.
                self._same_controller()
                before = self.runtime.coordinator.transport.status()
                self.bridge.fence_top()
                after = self.runtime.coordinator.transport.status()
                if (not self.runtime.reconciled_for_current_boot() or after.get('fenced') is not True or
                        any(before.get(k) != after.get(k) for k in ('epoch', 'boot', 'watermark'))):
                    raise ControllerUnavailable('promotion recovery fence not confirmed')
                rows = self.db.execute("SELECT o.marker,o.uid,o.scheduler_id FROM top_pending_promotion_v2_observed o JOIN jobs j ON j.id=o.marker WHERE o.request_id=? AND j.priority_marker_required=1", (request_id,)).fetchall()
                for marker, uid, sid in rows:
                    try:
                        self.bridge.pending_binding_receipt(uid, marker, sid)
                    except ControllerUnavailable:
                        try:
                            generation = self._reconcile_running_generation_locked(uid, marker, sid)
                        except ControllerUnavailable:
                            # RELEASE2 plus node proof may settle a task that
                            # completed between the clear and recovery. Never
                            # infer a terminal result from missing live state.
                            try:
                                receipt = self._reconcile_terminal_receipt_locked(uid, marker, sid)
                            except ControllerUnavailable:
                                self._recover_v2_cancelled_locked(request_id, marker, uid, sid)
                                continue
                            self.db.execute('BEGIN IMMEDIATE')
                            try:
                                observed = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,restart,generation FROM top_pending_promotion_v2_observed WHERE request_id=? AND marker=?', (request_id, marker)).fetchone()
                                local = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,generation,final_state,exit_code FROM top_terminal_receipts WHERE marker=?', (marker,)).fetchone()
                                expected = (uid, sid, receipt['submit'], str(receipt['sluid']), receipt['generation'], receipt['state'], receipt['exit_code'])
                                if (observed is None or tuple(observed[:5]) != (uid, sid, receipt['submit'], str(receipt['sluid']), 0) or
                                        observed[5] not in ('0', str(receipt['generation'])) or local is None or tuple(local) != expected):
                                    raise ControllerUnavailable('terminal promotion observation differs')
                                item = (request_id, marker, uid, sid, receipt['submit'], str(receipt['sluid']), str(receipt['generation']), receipt['state'], receipt['exit_code'])
                                prior = self.db.execute('SELECT * FROM top_pending_promotion_v2_terminal WHERE request_id=? AND marker=?', (request_id, marker)).fetchone()
                                if prior is None:
                                    self.db.execute('INSERT INTO top_pending_promotion_v2_terminal VALUES(?,?,?,?,?,?,?,?,?)', item)
                                elif tuple(prior) != item:
                                    raise ControllerUnavailable('terminal promotion observation conflicts')
                                self.db.execute('COMMIT')
                            except BaseException:
                                if self.db.in_transaction: self.db.execute('ROLLBACK')
                                raise
                            continue
                        self.db.execute("UPDATE top_pending_promotion_v2_observed SET state='running',generation=? WHERE request_id=? AND marker=? AND ((state='pending' AND generation='0') OR (state='running' AND generation=?))", (str(generation), request_id, marker, str(generation)))
                self._fence_pending_promotion_v2_locked(request_id)
                self._request_pending_v2_policy_locked(request_id)
                self._adopt_pending_v2_waiting_locked(request_id)
                self._adopt_pending_v2_submitted_locked(request_id)
                self._verify_v2_adopted_locked(request_id)
                self._complete_pending_v2_locked(request_id)
        self.runtime.recover_policy()
        with self.runtime.coordinator.serialized():
            promotions = [row[0] for row in self.db.execute(
                "SELECT request_id FROM top_promotion_batches WHERE state!='complete' ORDER BY request_id").fetchall()]
        promotion_errors = []
        recovered_promotions = 0
        for request_id in promotions:
            try:
                with self.runtime.coordinator.serialized():
                    result = self._resume_promotion_locked(request_id)
                if result.get('operation', {}).get('state') != 'committed':
                    raise ControllerUnavailable('promotion policy remains pending')
                recovered_promotions += 1
            except Exception as exc:
                promotion_errors.append(exc)
        with self.runtime.coordinator.serialized():
            orphan_markers = [row[0] for row in self.db.execute(
                "SELECT marker FROM top_orphan_cancellations WHERE state='requested' ORDER BY marker").fetchall()]
        for marker in orphan_markers:
            try:
                with self.runtime.coordinator.serialized():
                    self._recover_orphan_cancellation_locked(marker)
                recovered_promotions += 1
            except Exception as exc:
                promotion_errors.append(exc)
        with self.runtime.coordinator.serialized():
            prepared = self.db.execute(
                "SELECT f.uid,f.username,f.marker,j.id FROM top_fence_intents f "
                "LEFT JOIN jobs j ON j.id=f.marker WHERE f.state='prepared' "
                "ORDER BY f.marker").fetchall()
        unresolved = promotion_errors
        recovered = recovered_promotions
        for uid, username, marker, job_id in prepared:
            if job_id is None:
                # The original submitter must retry the same request; an orphan
                # does not contain enough information to recreate the job.
                unresolved.append(ControllerUnavailable('fenced submission has no job'))
                continue
            try:
                self.adopt_waiting(uid, username, marker)
                recovered += 1
            except Exception as exc:
                unresolved.append(exc)
        with self.runtime.coordinator.serialized():
            markers = [row[0] for row in self.db.execute(
                "SELECT marker FROM top_pending_intents WHERE state='pending' ORDER BY marker").fetchall()]
        for marker in markers:
            try:
                self.recover_marker(marker)
                recovered += 1
            except Exception as exc:
                unresolved.append(exc)
        if unresolved:
            raise ControllerOutcomeUnknown('priority recovery has unresolved intents') from unresolved[0]
        return recovered

    def migrate_proven_legacy_markers(self):
        """Migrate unambiguous pre-BIND2 ordinary rows under the writer lock."""
        with self.runtime.coordinator.serialized():
            self.db.execute('BEGIN IMMEDIATE')
            try:
                # Version 1 is assigned only during the serialized migration of
                # pre-BIND2 BOUND/CLOSED reservations. The legacy database is
                # authoritative here; this digest is not an independent receipt.
                rows = self.db.execute(
                    "SELECT j.id,j.owner_uid,j.name,j.requested_gpu,j.requested_cpu,j.requested_mem_mb,"
                    "j.requested_minutes,j.qos,j.workdir,j.env_name,j.command,j.output_path,"
                    "j.selected_tier,j.temporary_application_id FROM jobs j "
                    "JOIN monthly_dispatch_holds h ON h.job_id=j.id AND h.uid=j.owner_uid "
                    "WHERE j.origin='platform' AND j.priority_marker_required IS NULL "
                    "AND j.effective_job_digest_version IS NULL AND j.effective_job_digest IS NULL "
                    "AND h.binding_version=1 AND h.state IN ('BOUND','CLOSED') "
                    "AND h.scheduler_id IS NOT NULL AND CAST(j.scheduler_id AS TEXT)=CAST(h.scheduler_id AS TEXT) "
                    "AND h.bind_submit IS NULL AND h.bind_sluid IS NULL "
                    "AND h.tier=j.selected_tier AND h.gpu=j.requested_gpu AND h.minutes=j.requested_minutes "
                    "AND NOT EXISTS (SELECT 1 FROM top_pending_intents i WHERE i.marker=j.id) "
                    "AND NOT EXISTS (SELECT 1 FROM top_fence_intents f WHERE f.marker=j.id)").fetchall()
                keys = ('name','gpu','cpu','memory_mb','time_minutes','qos','workdir',
                        'environment','command','output_path','selected_tier','temporary_application_id')
                for row in rows:
                    marker, uid = row[:2]
                    if any(v is None for v in row[2:12]):
                        continue
                    payload = {'version': 1, 'job_id': marker, 'uid': uid, **dict(zip(keys, row[2:]))}
                    try:
                        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                                separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
                    except (TypeError, ValueError, OverflowError, UnicodeError):
                        continue
                    self.db.execute("UPDATE jobs SET priority_marker_required=0,effective_job_digest_version=1,"
                                    "effective_job_digest=? WHERE id=? AND priority_marker_required IS NULL "
                                    "AND effective_job_digest_version IS NULL AND effective_job_digest IS NULL", (digest, marker))
                self.db.execute('COMMIT')
                bound_legacy = self.db.execute(
                    "SELECT j.id,j.owner_uid,j.owner_name,j.scheduler_id FROM jobs j "
                    "JOIN monthly_dispatch_holds h ON h.job_id=j.id AND h.uid=j.owner_uid "
                    "WHERE j.origin='platform' AND j.state IN ('PENDING','RUNNING') "
                    "AND j.priority_marker_required IS NULL AND j.effective_job_digest_version=1 "
                    "AND j.effective_job_digest IS NOT NULL AND h.binding_version=1 AND h.state='BOUND' "
                    "AND h.bind_submit IS NULL AND h.bind_sluid IS NULL AND h.scheduler_id IS NOT NULL "
                    "AND CAST(j.scheduler_id AS TEXT)=CAST(h.scheduler_id AS TEXT) "
                    "AND h.tier=j.selected_tier AND h.gpu=j.requested_gpu AND h.minutes=j.requested_minutes "
                    "AND NOT EXISTS (SELECT 1 FROM top_pending_intents i WHERE i.marker=j.id) "
                    "AND NOT EXISTS (SELECT 1 FROM top_fence_intents f WHERE f.marker=j.id)").fetchall()
                for marker, uid, username, scheduler_id in bound_legacy:
                    def classify_bound(policy, snapshot):
                        top = policy.get('enabled') is True
                        if top:
                            self._same_controller()
                            self.bridge.fence_top()
                            snapshot = self._refresh_after_controller_write(policy, snapshot)
                        elif policy.get('enabled') is not False:
                            return
                        self._same_controller()
                        status = self.reservation_status_for_job(uid, marker)
                        if (status.get('scheduler_id') != int(scheduler_id) or
                                status.get('state') not in ('bound', 'active') or
                                (top and status.get('state') != 'active') or
                                self.runtime.coordinator.transport.status() != snapshot):
                            raise ControllerUnavailable('legacy controller reservation differs')
                        self.db.execute('BEGIN IMMEDIATE')
                        try:
                            row = self.db.execute(
                                "SELECT j.effective_job_digest,j.state FROM jobs j JOIN monthly_dispatch_holds h ON h.job_id=j.id "
                                "WHERE j.id=? AND j.owner_uid=? AND j.owner_name=? AND j.origin='platform' "
                                "AND j.state IN ('PENDING','RUNNING') AND j.priority_marker_required IS NULL "
                                "AND j.effective_job_digest_version=1 AND CAST(j.scheduler_id AS TEXT)=? "
                                "AND h.uid=j.owner_uid AND h.binding_version=1 AND h.state='BOUND' "
                                "AND CAST(h.scheduler_id AS TEXT)=? AND h.bind_submit IS NULL AND h.bind_sluid IS NULL "
                                "AND h.tier=j.selected_tier AND h.gpu=j.requested_gpu AND h.minutes=j.requested_minutes "
                                "AND NOT EXISTS (SELECT 1 FROM top_pending_intents i WHERE i.marker=j.id) "
                                "AND NOT EXISTS (SELECT 1 FROM top_fence_intents f WHERE f.marker=j.id)",
                                (marker, uid, username, str(scheduler_id), str(scheduler_id))).fetchone()
                            if (row is None or (top and row[1] != 'RUNNING') or
                                    not self._verify_effective_job(uid, marker, row[0]) or
                                    self.runtime.coordinator.transport.status() != snapshot or
                                    not self.runtime.reconciled_for_current_boot()):
                                raise ControllerUnavailable('legacy task changed')
                            self.db.execute('UPDATE jobs SET priority_marker_required=0 WHERE id=? AND priority_marker_required IS NULL', (marker,))
                            self.db.execute('COMMIT')
                        except BaseException:
                            if self.db.in_transaction: self.db.execute('ROLLBACK')
                            raise
                    try:
                        self.runtime._with_verified_policy_locked(uid, username, classify_bound)
                    except (ControllerUnavailable, ValueError, TypeError):
                        continue
                waiting = self.db.execute(
                    "SELECT id,owner_uid,owner_name FROM jobs WHERE origin='platform' AND state='WAITING_QUOTA' "
                    "AND scheduler_id IS NULL AND priority_marker_required IS NULL "
                    "AND effective_job_digest_version IS NULL AND effective_job_digest IS NULL").fetchall()
                for marker, uid, username in waiting:
                    def migrate_waiting(policy, status):
                        top = policy.get('enabled') is True
                        if top:
                            self._same_controller()
                            self.bridge.fence_top()
                            status = self._refresh_after_controller_write(policy, status)
                        elif policy.get('enabled') is not False:
                            return
                        migrated = False
                        self.db.execute('BEGIN IMMEDIATE')
                        try:
                            row = self.db.execute(
                                "SELECT j.name,j.requested_gpu,j.requested_cpu,j.requested_mem_mb,j.requested_minutes,"
                                "j.qos,j.workdir,j.env_name,j.command,j.output_path,j.selected_tier,j.temporary_application_id "
                                "FROM jobs j WHERE j.id=? AND j.owner_uid=? AND j.owner_name=? "
                                "AND j.origin='platform' AND j.state='WAITING_QUOTA' AND j.scheduler_id IS NULL "
                                "AND j.priority_marker_required IS NULL AND j.effective_job_digest_version IS NULL "
                                "AND j.effective_job_digest IS NULL AND j.selected_tier IN ('debug','important','normal') "
                                "AND NOT EXISTS (SELECT 1 FROM top_pending_intents i WHERE i.marker=j.id) "
                                "AND NOT EXISTS (SELECT 1 FROM top_fence_intents f WHERE f.marker=j.id) "
                                "AND NOT EXISTS (SELECT 1 FROM monthly_dispatch_holds h WHERE h.job_id=j.id)",
                                (marker, uid, username)).fetchone()
                            if (row is not None and all(v is not None for v in row[:10]) and
                                    self.runtime.coordinator.transport.status() == status and
                                    self.runtime.reconciled_for_current_boot()):
                                keys = ('name','gpu','cpu','memory_mb','time_minutes','qos','workdir',
                                        'environment','command','output_path','selected_tier','temporary_application_id')
                                payload = {'version': 1, 'job_id': marker, 'uid': uid, **dict(zip(keys, row))}
                                digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                                        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
                                self.db.execute("UPDATE jobs SET priority_marker_required=0,effective_job_digest_version=1,"
                                                "effective_job_digest=? WHERE id=? AND priority_marker_required IS NULL "
                                                "AND effective_job_digest_version IS NULL AND effective_job_digest IS NULL", (digest, marker))
                                migrated = True
                            self.db.execute('COMMIT')
                        except BaseException:
                            if self.db.in_transaction: self.db.execute('ROLLBACK')
                            raise
                        if top and migrated:
                            self._adopt_waiting_with_policy(uid, username, marker, policy, status)
                    try:
                        self.runtime._with_verified_policy_locked(uid, username, migrate_waiting)
                    except ControllerUnavailable:
                        continue
                digest_rows = self.db.execute(
                    "SELECT id,owner_uid,owner_name FROM jobs WHERE origin='platform' AND state='WAITING_QUOTA' "
                    "AND scheduler_id IS NULL AND priority_marker_required IS NULL AND effective_job_digest_version=1 "
                    "AND effective_job_digest IS NOT NULL AND selected_tier IN ('debug','important','normal')").fetchall()
                for marker, uid, username in digest_rows:
                    def classify_existing(policy, status):
                        top = policy.get('enabled') is True
                        if top:
                            self._same_controller()
                            self.bridge.fence_top()
                            status = self._refresh_after_controller_write(policy, status)
                        self.db.execute('BEGIN IMMEDIATE')
                        try:
                            row = self.db.execute(
                                "SELECT effective_job_digest FROM jobs j WHERE j.id=? AND j.owner_uid=? AND j.owner_name=? "
                                "AND j.origin='platform' AND j.state='WAITING_QUOTA' AND j.scheduler_id IS NULL "
                                "AND j.priority_marker_required IS NULL AND j.effective_job_digest_version=1 "
                                "AND NOT EXISTS (SELECT 1 FROM top_pending_intents i WHERE i.marker=j.id) "
                                "AND NOT EXISTS (SELECT 1 FROM top_fence_intents f WHERE f.marker=j.id) "
                                "AND NOT EXISTS (SELECT 1 FROM monthly_dispatch_holds h WHERE h.job_id=j.id)",
                                (marker, uid, username)).fetchone()
                            if (row is None or not self._verify_effective_job(uid, marker, row[0]) or
                                    self.runtime.coordinator.transport.status() != status or
                                    not self.runtime.reconciled_for_current_boot()):
                                raise ControllerUnavailable('legacy waiting identity changed')
                            self.db.execute("UPDATE jobs SET priority_marker_required=0 WHERE id=? AND priority_marker_required IS NULL", (marker,))
                            self.db.execute('COMMIT')
                        except BaseException:
                            if self.db.in_transaction: self.db.execute('ROLLBACK')
                            raise
                        if top:
                            self._adopt_waiting_with_policy(uid, username, marker, policy, status)
                    try:
                        self.runtime._with_verified_policy_locked(uid, username, classify_existing)
                    except ControllerUnavailable:
                        continue
                # Recover a crash after ordinary classification but before
                # the durable promotion intent was written.
                stranded = self.db.execute(
                    "SELECT j.id,j.owner_uid,j.owner_name FROM jobs j WHERE j.origin='platform' "
                    "AND j.state='WAITING_QUOTA' AND j.scheduler_id IS NULL AND j.priority_marker_required=0 "
                    "AND j.effective_job_digest_version=1 AND j.selected_tier IN ('debug','important','normal') "
                    "AND NOT EXISTS (SELECT 1 FROM top_pending_intents i WHERE i.marker=j.id) "
                    "AND NOT EXISTS (SELECT 1 FROM top_fence_intents f WHERE f.marker=j.id)").fetchall()
                for marker, uid, username in stranded:
                    def recover_adoption(policy, status):
                        if policy.get('enabled') is True:
                            self._adopt_waiting_with_policy(uid, username, marker, policy, status)
                    try:
                        self.runtime._with_verified_policy_locked(uid, username, recover_adoption)
                    except ControllerUnavailable:
                        continue
                candidates = self.db.execute(
                    "SELECT j.id,j.owner_uid,j.owner_name FROM jobs j "
                    "JOIN top_pending_intents i ON i.marker=j.id JOIN top_fence_intents f ON f.marker=j.id "
                    "WHERE j.origin='platform' AND j.priority_marker_required IS NULL "
                    "AND j.effective_job_digest_version IS NULL AND j.effective_job_digest IS NULL").fetchall()
                for marker, uid, username in candidates:
                    def migrate(policy, status):
                        if policy.get('enabled') is not True:
                            return
                        self.db.execute('BEGIN IMMEDIATE')
                        try:
                            row = self.db.execute(
                                "SELECT f.effective_digest FROM jobs j JOIN top_pending_intents i ON i.marker=j.id "
                                "JOIN top_fence_intents f ON f.marker=j.id WHERE j.id=? AND j.owner_uid=? "
                                "AND j.owner_name=? AND j.origin='platform' AND j.priority_marker_required IS NULL "
                                "AND j.effective_job_digest_version IS NULL AND j.effective_job_digest IS NULL "
                                "AND i.uid=j.owner_uid AND f.uid=j.owner_uid AND f.username=j.owner_name "
                                "AND i.account_generation=? AND f.account_generation=i.account_generation "
                                "AND i.request_id=f.request_id AND i.request_id=j.client_request_id "
                                "AND i.request_hash=f.request_hash AND i.request_hash=j.client_request_hash "
                                "AND i.state='registered' AND f.state='linked'",
                                (marker, uid, username, policy['identity_generation'])).fetchone()
                            if (row is not None and self._verify_effective_job(uid, marker, row[0]) and
                                    self.runtime.coordinator.transport.status() == status and
                                    self.runtime.reconciled_for_current_boot()):
                                self.db.execute("UPDATE jobs SET priority_marker_required=1,effective_job_digest_version=1,"
                                                "effective_job_digest=? WHERE id=? AND priority_marker_required IS NULL "
                                                "AND effective_job_digest_version IS NULL AND effective_job_digest IS NULL", (row[0], marker))
                            self.db.execute('COMMIT')
                        except BaseException:
                            if self.db.in_transaction: self.db.execute('ROLLBACK')
                            raise
                    try:
                        self.runtime._with_verified_policy_locked(uid, username, migrate)
                    except ControllerUnavailable:
                        continue
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
                raise

    def _has_marker_locked(self, marker):
        self.runtime.coordinator.assert_serialized()
        # One statement gives a single SQLite snapshot even outside a write transaction.
        row = self.db.execute(
            'SELECT j.owner_uid,j.owner_name,j.client_request_id,j.client_request_hash,'
            'j.effective_job_digest_version,j.effective_job_digest,j.priority_marker_required,'
            'i.uid,i.account_generation,i.request_id,i.request_hash,i.state,'
            'f.uid,f.username,f.account_generation,f.request_id,f.request_hash,f.effective_digest,f.state '
            'FROM jobs j LEFT JOIN top_pending_intents i ON i.marker=j.id '
            'LEFT JOIN top_fence_intents f ON f.marker=j.id WHERE j.id=?', (marker,)).fetchone()
        if row is None:
            orphan = self.db.execute('SELECT 1 FROM top_pending_intents WHERE marker=? '
                                     'UNION ALL SELECT 1 FROM top_fence_intents WHERE marker=? LIMIT 1',
                                     (marker, marker)).fetchone()
            if orphan is not None:
                raise ControllerUnavailable('priority intent has no task')
            return False
        uid, username, request_id, request_hash, version, digest, classification = row[:7]
        intent, fence = row[7:12], row[12:]
        if classification == 0 and all(v is None for v in intent) and all(v is None for v in fence):
            return False
        if classification != 1 or intent[0] is None or fence[0] is None or version != 1:
            raise ControllerUnavailable('priority task marker is unavailable')
        i_uid, generation, i_request, i_hash, state = intent
        expected_fence_state = 'closed' if state == 'cancelled' else 'linked'
        if (state not in ('pending', 'registered', 'cancel_pending', 'cancelled') or
                (i_uid, i_request, i_hash) != (uid, request_id, request_hash) or
                tuple(fence) != (uid, username, generation, request_id, request_hash, digest, expected_fence_state)):
            raise ControllerUnavailable('priority task identities differ')
        return True

    def has_marker(self, marker):
        with self.runtime.coordinator.serialized():
            return self._has_marker_locked(marker)

    def begin_cancel_waiting(self, marker, target, now):
        """Atomically fence broker submission and record marker cancellation."""
        if target not in ('CANCELLED', 'EXPIRED'):
            raise ValueError('invalid cancellation target')
        with self.runtime.coordinator.serialized():
            self.db.execute('BEGIN IMMEDIATE')
            try:
                changed = self.begin_cancel_in_transaction(marker, target, now)
                self.db.execute('COMMIT')
                return changed is True
            except BaseException as exc:
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
                raise ControllerOutcomeUnknown('priority cancellation intent needs reconciliation') from exc

    def begin_cancel_in_transaction(self, marker, target, now, *, promotion_request=None):
        """Join the caller's transaction under the coordinator lock.

        Return None when this is not a priority marker, allowing the caller to
        apply the ordinary cancellation path in the same transaction.
        """
        self.runtime.coordinator.assert_serialized()
        if not self.db.in_transaction or target not in ('CANCELLED', 'EXPIRED'):
            raise ValueError('locked cancellation transaction required')
        batch = self.db.execute("SELECT b.request_id,m.client_request_id,m.client_request_hash,m.effective_digest "
                                "FROM top_promotion_batches b JOIN top_promotion_members m ON m.request_id=b.request_id "
                                "JOIN jobs j ON j.id=m.marker AND j.owner_uid=b.uid "
                                "WHERE j.id=? AND b.state!='complete'", (marker,)).fetchone()
        open_batch = self.db.execute("SELECT 1 FROM top_promotion_batches b JOIN jobs j ON j.owner_uid=b.uid "
                                     "WHERE j.id=? AND b.state!='complete' LIMIT 1", (marker,)).fetchone()
        if open_batch:
            if batch is None:
                raise ControllerUnavailable('task is absent from the promotion batch')
            request_id, client_id, client_hash, digest = batch
            existing = self.db.execute('SELECT target,state FROM top_promotion_cancellations WHERE request_id=? AND marker=?',
                                       (request_id, marker)).fetchone()
            if promotion_request is not None:
                if (promotion_request != request_id or existing is None or tuple(existing) != (target, 'requested')):
                    raise ControllerUnavailable('promotion cancellation authorization differs')
                # Continue below: adoption must have registered the marker.
            else:
                job = self.db.execute('SELECT owner_uid,client_request_id,client_request_hash,effective_job_digest,'
                                      'state,scheduler_id FROM jobs WHERE id=?', (marker,)).fetchone()
                if (job is None or tuple(job[1:]) != (client_id, client_hash, digest, 'WAITING_QUOTA', None) or
                        not self._verify_effective_job(job[0], marker, digest)):
                    raise ControllerUnavailable('promotion cancellation identity changed')
                if existing is None:
                    self.db.execute("INSERT INTO top_promotion_cancellations VALUES(?,?,?,'requested')", (request_id, marker, target))
                elif tuple(existing) != (target, 'requested') and not (tuple(existing) == ('CANCELLED', 'requested') and target == 'EXPIRED'):
                    raise ControllerUnavailable('promotion cancellation target conflicts')
                if self.db.execute("UPDATE jobs SET reason_code='promotion_cancel_pending',reason_text='取消请求已记录，等待优先级恢复',updated_at=? "
                                   "WHERE id=? AND state='WAITING_QUOTA' AND scheduler_id IS NULL", (now, marker)).rowcount != 1:
                    raise ControllerUnavailable('promotion task changed during cancellation request')
                return True
        orphan = self.db.execute('SELECT j.owner_uid,j.owner_name,j.client_request_id,j.client_request_hash,'
                                 'j.effective_job_digest,j.priority_marker_required,j.state,j.scheduler_id,'
                                 'f.uid,f.username,f.account_generation,f.request_id,f.request_hash,f.effective_digest,f.state '
                                 'FROM jobs j JOIN top_fence_intents f ON f.marker=j.id '
                                 'LEFT JOIN top_pending_intents i ON i.marker=j.id '
                                 "WHERE j.id=? AND f.state='prepared' AND i.marker IS NULL", (marker,)).fetchone()
        if orphan is not None:
            uid, name, client_id, client_hash, digest, classification, state, sid = orphan[:8]
            f_uid, f_name, generation, f_request, f_hash, f_digest, f_state = orphan[8:]
            if ((uid, name, client_id, client_hash, digest) != (f_uid, f_name, f_request, f_hash, f_digest) or
                    classification != 0 or state != 'WAITING_QUOTA' or sid is not None or
                    not self._verify_effective_job(uid, marker, digest)):
                raise ControllerUnavailable('prepared cancellation identity changed')
            identity = (marker, uid, name, generation, client_id, client_hash, digest, target, 'requested')
            existing = self.db.execute('SELECT marker,uid,username,account_generation,request_id,request_hash,'
                                       'effective_digest,target,state FROM top_orphan_cancellations WHERE marker=?', (marker,)).fetchone()
            if existing is None:
                self.db.execute('INSERT INTO top_orphan_cancellations VALUES(?,?,?,?,?,?,?,?,?)', identity)
            elif tuple(existing) != identity and not (tuple(existing[:7]) == identity[:7] and tuple(existing[7:]) == ('CANCELLED','requested') and target == 'EXPIRED'):
                raise ControllerUnavailable('prepared cancellation target conflicts')
            if self.db.execute("UPDATE jobs SET reason_code='priority_cancel_pending',reason_text='取消请求已记录，等待优先级恢复',updated_at=? "
                               "WHERE id=? AND state='WAITING_QUOTA' AND scheduler_id IS NULL", (now, marker)).rowcount != 1:
                raise ControllerUnavailable('prepared task changed during cancellation request')
            return True
        if not self._has_marker_locked(marker):
            return None
        row = self.db.execute('SELECT j.state,j.scheduler_id,j.cancel_target,i.state '
                              'FROM jobs j JOIN top_pending_intents i ON i.marker=j.id WHERE j.id=?',
                              (marker,)).fetchone()
        if row is None:
            raise ControllerUnavailable('priority task marker changed')
        job_state, sid, old_target, marker_state = row
        if job_state == 'CANCELLING_RESERVATION' and sid is None and old_target == target and marker_state == 'cancel_pending':
            return True
        if job_state != 'WAITING_QUOTA' or sid is not None or marker_state not in ('pending', 'registered'):
            raise ControllerUnavailable('task can no longer be cancelled as unstarted')
        if self.db.execute("UPDATE jobs SET state='CANCELLING_RESERVATION',cancel_target=?,reason_code='cancelling',"
                           "reason_text='正在核对优先任务取消',updated_at=? WHERE id=? AND state='WAITING_QUOTA' AND scheduler_id IS NULL",
                           (target, now, marker)).rowcount != 1:
            raise ControllerUnavailable('task changed during cancellation')
        if self.db.execute("UPDATE top_pending_intents SET state='cancel_pending' WHERE marker=? AND state IN ('pending','registered')",
                           (marker,)).rowcount != 1:
            raise ControllerUnavailable('marker changed during cancellation')
        return True

    def finish_cancel_waiting(self, marker, now):
        """Confirm an unbound tombstone after monthly reservation cancellation."""
        with self.runtime.coordinator.serialized():
            return self._finish_cancel_waiting_locked(marker, now)

    def _finish_cancel_waiting_locked(self, marker, now):
        self.runtime.coordinator.assert_serialized()
        query = ('SELECT j.owner_uid,j.owner_name,j.state,j.scheduler_id,j.cancel_target,'
                 'i.account_generation,i.request_id,i.request_hash,i.state,j.effective_job_digest '
                 'FROM jobs j JOIN top_pending_intents i ON i.marker=j.id WHERE j.id=?')
        snapshot_queries = (
            'SELECT * FROM jobs WHERE id=?',
            'SELECT * FROM top_pending_intents WHERE marker=?',
            'SELECT * FROM top_fence_intents WHERE marker=?',
        )
        def snapshot():
            rows = [self.db.execute(sql, (marker,)).fetchone() for sql in snapshot_queries]
            if any(item is None for item in rows):
                raise ControllerUnavailable('priority cancellation identity missing')
            return tuple(tuple(item) for item in rows)
        self.db.execute('BEGIN')
        try:
            row = self.db.execute(query, (marker,)).fetchone()
            if row is None:
                self.db.execute('COMMIT')
                return False
            uid, username, state, sid, target, generation, request_id, request_hash, marker_state, digest = row
            if (state != 'CANCELLING_RESERVATION' or sid is not None or target not in ('CANCELLED','EXPIRED') or
                    marker_state != 'cancel_pending' or not self._has_marker_locked(marker) or
                    not self._verify_effective_job(uid, marker, digest)):
                raise ControllerUnavailable('priority cancellation is not ready')
            before = snapshot()
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

        def finish(policy, status):
            if policy['enabled'] is not True or policy['identity_generation'] != generation:
                raise ControllerUnavailable('priority policy changed during cancellation')
            self._same_controller()
            self.bridge.fence_top()
            status = self._refresh_after_controller_write(policy, status)
            reservation = self.reservation_status_for_job(uid, marker)
            if (not reservation.get('present') or reservation.get('state') != 'cancelled' or
                    reservation.get('scheduler_id') is not None or reservation.get('generation') is not None):
                raise ControllerUnavailable('unstarted reservation cancellation not confirmed')
            identity = (marker, uid, generation, request_id, request_hash)
            if self.bridge.cancel_unbound_top(*identity) != {'cancelled': True, 'identity': identity}:
                raise ControllerOutcomeUnknown('priority cancellation receipt differs')
            status = self._refresh_after_controller_write(policy, status)
            self.db.execute('BEGIN IMMEDIATE')
            try:
                current = self.db.execute(query, (marker,)).fetchone()
                local_hold = self.db.execute('SELECT uid,state,scheduler_id FROM monthly_dispatch_holds WHERE job_id=?', (marker,)).fetchone()
                if local_hold is not None and tuple(local_hold) != (uid, 'CANCELLED', None):
                    raise ControllerOutcomeUnknown('local reservation cancellation differs')
                if (current is None or tuple(current) != tuple(row) or snapshot() != before or
                        not self._has_marker_locked(marker) or
                        not self._verify_effective_job(uid, marker, digest)):
                    raise ControllerOutcomeUnknown('priority cancellation task changed')
                if self.db.execute("UPDATE top_pending_intents SET state='cancelled' WHERE marker=? AND state='cancel_pending'", (marker,)).rowcount != 1:
                    raise ControllerOutcomeUnknown('priority marker changed during cancellation')
                if self.db.execute("UPDATE top_fence_intents SET state='closed' WHERE marker=? AND state='linked'", (marker,)).rowcount != 1:
                    raise ControllerOutcomeUnknown('priority fence intent changed during cancellation')
                if self.db.execute("UPDATE jobs SET state=?,reason_code=?,reason_text=?,updated_at=?,finished_at=? "
                                   "WHERE id=? AND state='CANCELLING_RESERVATION' AND scheduler_id IS NULL AND cancel_target=?",
                                   (target, 'approval_expired' if target == 'EXPIRED' else 'cancelled_by_user',
                                    '优先任务取消已确认', now, now, marker, target)).rowcount != 1:
                    raise ControllerOutcomeUnknown('priority job changed during cancellation')
                self.db.execute('COMMIT')
            except BaseException as exc:
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
                raise ControllerOutcomeUnknown('priority cancellation confirmation needs reconciliation') from exc
            return True

        return self.runtime._with_verified_policy_locked(uid, username, finish)


    def claim_for_dispatch(self, uid, username, marker, effective_digest, claim):
        """Check the priority barrier and claim a job under the same lock.

        A returned top job must be submitted on hold until controller binding
        and the yet-unimplemented safe release have succeeded.
        """
        def apply(policy, status):
            # Recovery scans release the coordinator between operations. Recheck
            # under the claim lock so a newly orphaned request cannot be missed.
            unresolved = self.db.execute(
                "SELECT 1 FROM top_fence_intents WHERE state='prepared' "
                "UNION ALL SELECT 1 FROM top_pending_intents WHERE state IN ('pending','cancel_pending') "
                "UNION ALL SELECT 1 FROM top_promotion_batches WHERE state!='complete' "
                "UNION ALL SELECT 1 FROM top_orphan_cancellations WHERE state='requested' LIMIT 1"
            ).fetchone()
            if unresolved is not None:
                raise ControllerUnavailable('priority recovery is incomplete')
            top = policy['enabled'] is True
            classification = self.db.execute('SELECT priority_marker_required FROM jobs WHERE id=? AND owner_uid=?',
                                             (marker, uid)).fetchone()
            if classification is None or classification[0] != (1 if top else 0):
                raise ControllerUnavailable('task priority classification requires reconciliation')
            intent = self.db.execute('SELECT i.uid,i.account_generation,i.state,f.state,f.effective_digest '
                                     'FROM top_pending_intents i JOIN top_fence_intents f ON f.marker=i.marker '
                                     'WHERE i.marker=?', (marker,)).fetchone()
            if top:
                expected = (uid, policy['identity_generation'], 'registered', 'linked', effective_digest)
                if intent is None or tuple(intent) != expected:
                    raise ControllerUnavailable('top task marker is not registered')
                self._same_controller()
                self.bridge.fence_top()
                status = self._refresh_after_controller_write(policy, status)
            else:
                if self.db.execute('SELECT 1 FROM top_pending_intents WHERE marker=? '
                                   'UNION ALL SELECT 1 FROM top_fence_intents WHERE marker=? LIMIT 1',
                                   (marker, marker)).fetchone() is not None:
                    raise ControllerUnavailable('ordinary task has a top marker')
                orphan = self.db.execute("SELECT 1 FROM top_pending_intents i LEFT JOIN top_fence_intents f "
                    "ON f.marker=i.marker WHERE i.state='registered' AND (f.marker IS NULL OR f.state!='linked') LIMIT 1").fetchone()
                if orphan is not None:
                    raise ControllerUnavailable('registered priority marker has no linked fence')
                waiting = False
                linked = self.db.execute("SELECT f.marker,j.owner_uid,j.state,j.effective_job_digest,i.state,j.scheduler_id "
                    "FROM top_fence_intents f LEFT JOIN jobs j ON j.id=f.marker "
                    "LEFT JOIN top_pending_intents i ON i.marker=f.marker WHERE f.state='linked'").fetchall()
                for pending_marker, pending_uid, pending_state, pending_digest, intent_state, scheduler_id in linked:
                    if (pending_state not in ('WAITING_QUOTA', 'SUBMITTING', 'PENDING', 'RUNNING', 'COMPLETING', 'COMPLETED', 'FAILED', 'CANCELLED')
                            or intent_state != 'registered' or not self._has_marker_locked(pending_marker)
                            or not self._verify_effective_job(pending_uid, pending_marker, pending_digest)):
                        raise ControllerUnavailable('linked priority task requires reconciliation')
                    binding = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,restart,generation '
                                              'FROM top_marker_bindings WHERE marker=?', (pending_marker,)).fetchone()
                    if pending_state in ('COMPLETED', 'FAILED', 'CANCELLED'):
                        terminal = self.db.execute(
                            'SELECT uid,scheduler_id,submit_time,sluid,generation,final_state FROM top_terminal_receipts WHERE marker=?',
                            (pending_marker,)).fetchone()
                        held = self.db.execute('SELECT uid,scheduler_id,state,binding_version,bind_submit,bind_sluid '
                                               'FROM monthly_dispatch_holds WHERE job_id=?', (pending_marker,)).fetchone()
                        if (binding is None or terminal is None or held is None or scheduler_id is None or
                                binding[0] != pending_uid or str(binding[1]) != str(scheduler_id) or binding[4] != 0 or
                                binding[5] <= 0 or tuple(terminal[:5]) != (binding[0], binding[1], binding[2], binding[3], binding[5]) or
                                {3: 'COMPLETED', 4: 'CANCELLED', 5: 'FAILED', 6: 'FAILED', 7: 'FAILED', 8: 'FAILED', 9: 'FAILED', 10: 'FAILED', 11: 'FAILED'}.get(terminal[5] & 0xff) != pending_state or
                                tuple(held) != (pending_uid, binding[1], 'CLOSED', 2, str(binding[2]), binding[3])):
                            raise ControllerUnavailable('terminal priority retirement requires reconciliation')
                        continue
                    if pending_state == 'WAITING_QUOTA':
                        if scheduler_id is not None or binding is not None:
                            raise ControllerUnavailable('waiting priority task has scheduler identity')
                    elif pending_state == 'PENDING':
                        if (scheduler_id is None or binding is None or str(binding[1]) != str(scheduler_id)
                                or binding[0] != pending_uid or type(binding[1]) is not int
                                or not 1 <= binding[1] <= 0xffffffff
                                or type(binding[2]) is not int or not 1 <= binding[2] <= 2**63 - 1
                                or not isinstance(binding[3], str) or not re.fullmatch(r'[1-9][0-9]{0,19}', binding[3])
                                or int(binding[3]) > 2**64 - 1 or tuple(binding[4:]) != (0, 0)):
                            raise ControllerUnavailable('pending priority task binding requires reconciliation')
                    elif pending_state == 'RUNNING':
                        started = self.db.execute(
                            'SELECT uid,scheduler_id,submit_time,sluid,generation FROM top_started_receipts WHERE marker=?',
                            (pending_marker,)).fetchone()
                        if (binding is None or started is None or scheduler_id is None or
                                str(binding[1]) != str(scheduler_id) or binding[0] != pending_uid or
                                binding[4] != 0 or type(binding[5]) is not int or binding[5] <= 0 or
                                tuple(started) != (binding[0], binding[1], binding[2], binding[3], binding[5])):
                            raise ControllerUnavailable('running priority receipt requires reconciliation')
                        continue
                    else:
                        raise ControllerUnavailable('priority task lifecycle requires controller proof')
                    waiting = True
                if waiting:
                    raise PriorityBarrierWait('top priority task is still waiting')
            if not claim():
                raise ControllerUnavailable('job changed before submission claim')
            return top
        return self.runtime.with_verified_policy(uid, username, apply)

    def cancel_submitted_pending(self, uid, marker, scheduler_id):
        """Journal a cancellation request and reconcile the same bound job."""
        with self.runtime.coordinator.serialized():
            if not self.runtime.reconciled_for_current_boot() or not self._has_marker_locked(marker):
                raise ControllerUnavailable('priority cancellation prerequisites unavailable')
            row = self.db.execute('SELECT uid,account_generation,request_id,request_hash,state FROM top_pending_intents WHERE marker=?', (marker,)).fetchone()
            binding = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,restart,generation FROM top_marker_bindings WHERE marker=?', (marker,)).fetchone()
            job = self.db.execute('SELECT owner_uid,state,scheduler_id,effective_job_digest FROM jobs WHERE id=?', (marker,)).fetchone()
            if (row is None or row[0] != uid or row[4] != 'registered' or binding is None or
                    binding[0] != uid or binding[1] != scheduler_id or binding[4] != 0 or binding[5] != 0 or
                    job is None or job[0] != uid or job[1] != 'PENDING' or str(job[2]) != str(scheduler_id) or
                    not self._verify_effective_job(uid, marker, job[3])):
                raise ControllerUnavailable('priority pending cancellation identity differs')
            intent = (marker, uid, scheduler_id, binding[2], binding[3])
            self.db.execute('BEGIN IMMEDIATE')
            try:
                existing = self.db.execute('SELECT marker,uid,scheduler_id,submit_time,sluid FROM top_submitted_cancel_intents WHERE marker=?', (marker,)).fetchone()
                if existing is None:
                    self.db.execute("INSERT INTO top_submitted_cancel_intents VALUES(?,?,?,?,?,'requested')", intent)
                elif tuple(existing) != intent:
                    raise ControllerUnavailable('pending cancellation conflicts')
                self.db.execute('COMMIT')
            except BaseException:
                if self.db.in_transaction: self.db.execute('ROLLBACK')
                raise
            self._same_controller()
            state = self.reservation_status_for_job(uid, marker)
            self.bridge.cancel_pending_top(marker, uid, row[1], row[2], row[3], scheduler_id, binding[2], int(binding[3]))
            state = self.reservation_status_for_job(uid, marker)
            if state.get('state') != 'cancelled_bound' or state.get('scheduler_id') != scheduler_id:
                raise ControllerOutcomeUnknown('pending cancellation not durably confirmed')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                current = self.db.execute('SELECT owner_uid,state,scheduler_id,effective_job_digest FROM jobs WHERE id=?', (marker,)).fetchone()
                if current is None or tuple(current) != tuple(job) or not self._has_marker_locked(marker):
                    raise ControllerUnavailable('pending cancellation changed')
                self.db.execute("UPDATE top_submitted_cancel_intents SET state='reservation_cancelled' WHERE marker=?", (marker,))
                if self.db.execute("UPDATE monthly_dispatch_holds SET state='CLOSED' WHERE job_id=? AND uid=? AND scheduler_id=? AND state IN ('BINDING','BOUND')", (marker, uid, scheduler_id)).rowcount != 1:
                    raise ControllerUnavailable('reservation changed during cancellation')
                if self.db.execute("UPDATE top_pending_intents SET state='cancelled' WHERE marker=? AND state='registered'", (marker,)).rowcount != 1:
                    raise ControllerUnavailable('priority intent changed during cancellation')
                if self.db.execute("UPDATE top_fence_intents SET state='closed' WHERE marker=? AND state='linked'", (marker,)).rowcount != 1:
                    raise ControllerUnavailable('priority fence changed during cancellation')
                now = dt.datetime.now(dt.timezone.utc).isoformat()
                if self.db.execute("UPDATE jobs SET state='CANCELLED',reason_code='cancelled_by_user',reason_text='已取消',updated_at=?,finished_at=? WHERE id=? AND state='PENDING'", (now, now, marker)).rowcount != 1:
                    raise ControllerUnavailable('priority task changed during cancellation')
                self.db.execute('COMMIT')
            except BaseException:
                if self.db.in_transaction: self.db.execute('ROLLBACK')
                raise
            return True

    def reconcile_terminal_receipt(self, uid, marker, scheduler_id):
        """Persist controller RELEASE2 evidence without inferring cancellation."""
        if type(uid) is not int or type(scheduler_id) is not int or scheduler_id <= 0:
            raise ControllerUnavailable('invalid terminal identity')
        with self.runtime.coordinator.serialized():
            return self._reconcile_terminal_receipt_locked(uid, marker, scheduler_id)

    def _reconcile_terminal_receipt_locked(self, uid, marker, scheduler_id):
        self.runtime.coordinator.assert_serialized()
        if not self.runtime.reconciled_for_current_boot():
            raise ControllerUnavailable('controller recovery incomplete')
        self._same_controller()
        before = self.runtime.coordinator.transport.status()
        receipt = self.bridge.top_terminal_receipt(uid, marker, scheduler_id)
        reservation = self.reservation_status_for_job(uid, marker)
        if (reservation.get('state') != 'released' or reservation.get('scheduler_id') != scheduler_id or
                reservation.get('generation') != receipt['generation'] or reservation.get('sluid') != receipt['sluid'] or
                (receipt['state'] & 0xff) not in (3, 4, 5, 6, 7, 8, 9, 10, 11)):
            raise ControllerUnavailable('terminal reservation outcome not confirmed')
        if self.runtime.coordinator.transport.status() != before or not self.runtime.reconciled_for_current_boot():
            raise ControllerUnavailable('controller changed during terminal recovery')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            binding = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,restart,generation '
                                      'FROM top_marker_bindings WHERE marker=?', (marker,)).fetchone()
            started = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,generation '
                                      'FROM top_started_receipts WHERE marker=?', (marker,)).fetchone()
            identity = (uid, scheduler_id, receipt['submit'], str(receipt['sluid']), receipt['generation'])
            if (binding is None or tuple(binding[:5]) != identity[:4] + (0,) or
                    binding[5] not in (0, identity[4]) or
                    (started is not None and tuple(started) != identity) or not self._has_marker_locked(marker)):
                raise ControllerUnavailable('terminal marker identity differs')
            if started is None:
                self.db.execute('INSERT INTO top_started_receipts VALUES(?,?,?,?,?,?)', (marker,) + identity)
            if binding[5] == 0:
                self.db.execute('UPDATE top_marker_bindings SET generation=? WHERE marker=? AND generation=0',
                                (identity[4], marker))
            result = (marker,) + identity + (receipt['state'], receipt['exit_code'])
            old = self.db.execute('SELECT marker,uid,scheduler_id,submit_time,sluid,generation,final_state,exit_code '
                                  'FROM top_terminal_receipts WHERE marker=?', (marker,)).fetchone()
            if old is None:
                self.db.execute('INSERT INTO top_terminal_receipts VALUES(?,?,?,?,?,?,?,?)', result)
            elif tuple(old) != result:
                raise ControllerUnavailable('terminal receipt conflicts')
            job = self.db.execute('SELECT owner_uid,scheduler_id,state,effective_job_digest FROM jobs WHERE id=?', (marker,)).fetchone()
            held = self.db.execute('SELECT uid,scheduler_id,state,binding_version,bind_submit,bind_sluid FROM monthly_dispatch_holds WHERE job_id=?', (marker,)).fetchone()
            terminal = {3: 'COMPLETED', 4: 'CANCELLED', 5: 'FAILED', 6: 'FAILED', 7: 'FAILED', 8: 'FAILED', 9: 'FAILED', 10: 'FAILED', 11: 'FAILED'}[receipt['state'] & 0xff]
            if (job is None or job[0] != uid or str(job[1]) != str(scheduler_id) or
                    job[2] not in ('SUBMITTING', 'PENDING', 'RUNNING', 'COMPLETING', 'SUSPENDED', terminal) or
                    not self._verify_effective_job(uid, marker, job[3]) or held is None or
                    tuple(held) not in ((uid, scheduler_id, 'BOUND', 2, str(receipt['submit']), str(receipt['sluid'])),
                                        (uid, scheduler_id, 'CLOSED', 2, str(receipt['submit']), str(receipt['sluid'])))):
                raise ControllerUnavailable('terminal reservation or job identity differs')
            self.db.execute("UPDATE monthly_dispatch_holds SET state='CLOSED' WHERE job_id=? AND state='BOUND'", (marker,))
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            self.db.execute("UPDATE jobs SET state=?,reason_code='finished',reason_text=?,updated_at=?,"
                            "finished_at=COALESCE(finished_at,?) WHERE id=?", (terminal, terminal, now, now, marker))
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise
        return receipt

    def recover_submission_identity(self, uid, marker):
        with self.runtime.coordinator.serialized():
            if not self.runtime.reconciled_for_current_boot() or not self._has_marker_locked(marker):
                raise ControllerUnavailable('priority submission recovery unavailable')
            row = self.db.execute('SELECT uid,account_generation,request_id,request_hash,state FROM top_pending_intents WHERE marker=?', (marker,)).fetchone()
            job = self.db.execute('SELECT owner_uid,state,scheduler_id,effective_job_digest FROM jobs WHERE id=?', (marker,)).fetchone()
            if (row is None or row[0] != uid or row[4] != 'registered' or job is None or job[0] != uid or
                    job[1] != 'SUBMITTING' or job[2] is not None or not self._verify_effective_job(uid, marker, job[3])):
                raise ControllerUnavailable('priority submission intent differs')
            self._same_controller()
            before = self.runtime.coordinator.transport.status()
            sid, submit, sluid, generation = self.bridge.lookup_top_submission(marker, uid, row[1], row[2], row[3])
            if self.runtime.coordinator.transport.status() != before or not self.runtime.reconciled_for_current_boot():
                raise ControllerUnavailable('controller changed during lookup')
            binding = (marker, uid, sid, submit, str(sluid), 0, generation)
            self.db.execute('BEGIN IMMEDIATE')
            try:
                current = self.db.execute('SELECT owner_uid,state,scheduler_id,effective_job_digest FROM jobs WHERE id=?', (marker,)).fetchone()
                if current is None or tuple(current) != tuple(job) or not self._has_marker_locked(marker):
                    raise ControllerUnavailable('priority submission changed during lookup')
                existing = self.db.execute('SELECT marker,uid,scheduler_id,submit_time,sluid,restart,generation FROM top_marker_bindings WHERE marker=?', (marker,)).fetchone()
                if existing is None:
                    self.db.execute('INSERT INTO top_marker_bindings VALUES(?,?,?,?,?,?,?)', binding)
                elif tuple(existing) != binding:
                    raise ControllerUnavailable('priority submission binding conflicts')
                self.db.execute('COMMIT')
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
                raise
            return str(sid)

    def reconcile_running_generation(self, uid, marker, scheduler_id):
        """Record a running allocation generation only for an exact bound task."""
        if type(uid) is not int or type(scheduler_id) is not int or scheduler_id <= 0:
            raise ControllerUnavailable('invalid running identity')
        with self.runtime.coordinator.serialized():
            return self._reconcile_running_generation_locked(uid, marker, scheduler_id)

    def _reconcile_running_generation_locked(self, uid, marker, scheduler_id):
        self.runtime.coordinator.assert_serialized()
        if not self.runtime.reconciled_for_current_boot():
            raise ControllerUnavailable('priority controller not reconciled')
        self._same_controller()
        before = self.runtime.coordinator.transport.status()
        receipt = self.bridge.binding_receipt(uid, marker, scheduler_id)
        status = self.reservation_status_for_job(uid, marker)
        running = self.bridge.top_running_receipt(uid, marker, scheduler_id)
        intent = self.db.execute('SELECT uid,account_generation,request_id,request_hash,state FROM top_pending_intents WHERE marker=?', (marker,)).fetchone()
        if intent is None or intent[0] != uid or intent[4] != 'registered':
            raise ControllerUnavailable('running registered request unavailable')
        located = self.bridge.lookup_top_submission(marker, uid, intent[1], intent[2], intent[3])
        if located != (scheduler_id, receipt['submit'], receipt['sluid'], running['generation']):
            raise ControllerUnavailable('running registered request identity differs')
        if (running.get('submit') != receipt.get('submit') or running.get('sluid') != receipt.get('sluid') or
                running.get('generation') != status.get('generation')):
            raise ControllerUnavailable('running marker and reservation differ')
        if (status.get('state') != 'active' or status.get('scheduler_id') != scheduler_id or
                status.get('sluid') != receipt.get('sluid') or
                type(status.get('generation')) is not int or status['generation'] <= 0 or
                status['generation'] > 2**63-1):
            raise ControllerUnavailable('running reservation not confirmed')
        if self.runtime.coordinator.transport.status() != before or not self.runtime.reconciled_for_current_boot():
            raise ControllerUnavailable('controller changed during running reconciliation')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            binding = self.db.execute('SELECT uid,scheduler_id,submit_time,sluid,restart,generation '
                                      'FROM top_marker_bindings WHERE marker=?', (marker,)).fetchone()
            job = self.db.execute('SELECT owner_uid,scheduler_id,priority_marker_required,state,effective_job_digest FROM jobs WHERE id=?',
                                  (marker,)).fetchone()
            expected = (uid, scheduler_id, receipt['submit'], str(receipt['sluid']), 0)
            if (job is None or job[0] != uid or job[1] not in (None, str(scheduler_id), scheduler_id) or job[2] != 1 or
                    job[3] not in ('SUBMITTING', 'PENDING', 'RUNNING') or
                    not self._verify_effective_job(uid, marker, job[4]) or not self._has_marker_locked(marker)):
                raise ControllerUnavailable('running marker identity differs')
            if binding is None:
                self.db.execute('INSERT INTO top_marker_bindings VALUES(?,?,?,?,?,?,?)',
                                (marker,) + expected + (status['generation'],))
            elif tuple(binding[:5]) != expected or binding[5] not in (0, status['generation']):
                raise ControllerUnavailable('running marker binding conflicts')
            else:
                self.db.execute('UPDATE top_marker_bindings SET generation=? WHERE marker=? AND generation=0',
                                (status['generation'], marker))
            hold = self.db.execute('SELECT uid,scheduler_id,state,binding_version,bind_submit,bind_sluid,tier,gpu,minutes '
                                   'FROM monthly_dispatch_holds WHERE job_id=?', (marker,)).fetchone()
            if (hold is None or hold[0] != uid or hold[1] not in (None, scheduler_id) or
                    hold[2] not in ('HELD', 'BINDING', 'BOUND') or hold[3] != 2 or
                    hold[4] not in (None, str(receipt['submit'])) or hold[5] not in (None, str(receipt['sluid'])) or
                    (hold[6], hold[7], hold[8]) != (status.get('tier'), status.get('gpu'), status.get('minutes'))):
                raise ControllerUnavailable('running reservation intent conflicts')
            self.db.execute("UPDATE monthly_dispatch_holds SET scheduler_id=?,state='BOUND',bind_submit=?,bind_sluid=? WHERE job_id=?",
                            (scheduler_id, str(receipt['submit']), str(receipt['sluid']), marker))
            started = (marker, uid, scheduler_id, receipt['submit'], str(receipt['sluid']), status['generation'])
            existing = self.db.execute('SELECT marker,uid,scheduler_id,submit_time,sluid,generation '
                                       'FROM top_started_receipts WHERE marker=?', (marker,)).fetchone()
            if existing is None:
                self.db.execute('INSERT INTO top_started_receipts VALUES(?,?,?,?,?,?)', started)
            elif tuple(existing) != started:
                raise ControllerUnavailable('running receipt conflicts')
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            changed = self.db.execute("UPDATE jobs SET state='RUNNING',reason_code='running',reason_text='运行中',"
                                      "started_at=COALESCE(started_at,?),updated_at=?,scheduler_id=? WHERE id=? AND state IN "
                                      "('SUBMITTING','PENDING','RUNNING') AND (scheduler_id IS NULL OR scheduler_id=?)",
                                      (now, now, str(scheduler_id), marker, str(scheduler_id))).rowcount
            if changed != 1:
                raise ControllerUnavailable('running task changed concurrently')
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise
        return status['generation']

    def bind_held(self, uid, username, marker, scheduler_id, *, readonly=False):
        """Bind the exact held scheduler instance; do not release its hold."""
        if type(scheduler_id) is not int or not 1 <= scheduler_id <= 0xffffffff:
            raise ValueError('invalid scheduler id')

        def apply(policy, status):
            identity_query = ('SELECT i.account_generation,i.request_id,i.request_hash,i.state,'
                                  'f.effective_digest,f.state,j.owner_uid,j.owner_name,j.state,j.scheduler_id,'
                                  'j.effective_job_digest_version,j.effective_job_digest '
                                  'FROM top_pending_intents i JOIN top_fence_intents f ON f.marker=i.marker '
                                  'JOIN jobs j ON j.id=i.marker WHERE i.marker=?')
            row = self.db.execute(identity_query, (marker,)).fetchone()
            if row is None:
                raise ControllerUnavailable('held task marker missing')
            generation, request_id, request_hash, marker_state, digest, fence_state, owner, name, state, sid, version, job_digest = row
            if (policy['enabled'] is not True or policy['identity_generation'] != generation or
                marker_state != 'registered' or fence_state != 'linked' or owner != uid or name != username or
                state not in ('SUBMITTING', 'PENDING') or str(sid) != str(scheduler_id) or
                version != 1 or digest != job_digest or not isinstance(digest, str) or
                not re.fullmatch(r'[0-9a-f]{64}', digest) or not self._has_marker_locked(marker) or
                not self._verify_effective_job(uid, marker, digest)):
                raise ControllerUnavailable('held task identity changed')
            reservation = self.db.execute(
                'SELECT uid,scheduler_id,state,bind_submit,bind_sluid FROM monthly_dispatch_holds WHERE job_id=?',
                (marker,)).fetchone()
            if (reservation is None or reservation[0] != uid or reservation[1] != scheduler_id or
                    reservation[2] != 'BOUND' or not reservation[3] or not reservation[4]):
                raise ControllerUnavailable('durable reservation identity missing')
            self._same_controller()
            receipt = (self.bridge.top_pending_receipt(uid, marker, scheduler_id) if readonly else
                       self.bridge.bind_top(marker, uid, generation, request_id, request_hash, scheduler_id))
            status = self._refresh_after_controller_write(policy, status)
            if (not isinstance(receipt, dict) or receipt.get('job_id') != scheduler_id or
                type(receipt.get('submit')) is not int or receipt['submit'] <= 0 or
                type(receipt.get('sluid')) is not int or receipt['sluid'] <= 0 or
                receipt.get('restart') != 0 or receipt.get('generation') != 0):
                raise ControllerOutcomeUnknown('held binding receipt differs')
            if (str(receipt['submit']), str(receipt['sluid'])) != (reservation[3], reservation[4]):
                raise ControllerOutcomeUnknown('priority and reservation binding identities differ')
            if receipt['submit'] > 2**63 - 1 or receipt['sluid'] > 2**64 - 1:
                raise ControllerOutcomeUnknown('held binding identity is out of range')
            if not self._verify_effective_job(uid, marker, digest):
                raise ControllerOutcomeUnknown('held task changed after controller binding')
            binding = (marker, uid, scheduler_id, receipt['submit'], str(receipt['sluid']), 0, 0)
            self.db.execute('BEGIN IMMEDIATE')
            try:
                current = self.db.execute(identity_query, (marker,)).fetchone()
                current_reservation = self.db.execute(
                    'SELECT uid,scheduler_id,state,bind_submit,bind_sluid FROM monthly_dispatch_holds WHERE job_id=?',
                    (marker,)).fetchone()
                if (current_reservation is None or tuple(current_reservation) != tuple(reservation) or
                        current is None or tuple(current) != tuple(row) or not self._has_marker_locked(marker) or
                        not self._verify_effective_job(uid, marker, digest)):
                    raise ControllerOutcomeUnknown('held task changed while binding')
                existing = self.db.execute('SELECT marker,uid,scheduler_id,submit_time,sluid,restart,generation '
                                           'FROM top_marker_bindings WHERE marker=?', (marker,)).fetchone()
                if existing is None:
                    self.db.execute('INSERT INTO top_marker_bindings VALUES(?,?,?,?,?,?,?)', binding)
                elif tuple(existing) != binding:
                    raise ControllerOutcomeUnknown('held binding conflicts')
                self.db.execute('COMMIT')
            except BaseException as exc:
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
                raise ControllerOutcomeUnknown('held binding persistence needs reconciliation') from exc
            return receipt

        return self.runtime.with_verified_policy(uid, username, apply)

    def _recover_marker_locked(self, marker):
        row = self.db.execute('SELECT i.uid,j.owner_name,i.account_generation,i.request_id,'
                              'i.request_hash,i.state,j.owner_uid,j.client_request_id,j.client_request_hash,'
                              'j.effective_job_digest_version,j.effective_job_digest,j.state,j.scheduler_id,j.cancel_target '
                              'FROM top_pending_intents i LEFT JOIN jobs j ON j.id=i.marker WHERE i.marker=?',
                              (marker,)).fetchone()
        if row is None:
            # Missing marker is only ordinary if the durable classification
            # and absence of a conflicting fence agree in the same snapshot.
            if self._has_marker_locked(marker):
                raise ControllerUnavailable('priority task marker is missing')
            return False
        uid, username, generation, request_id, request_hash, state, job_uid, job_request, job_hash, digest_version, digest, job_state, scheduler_id, cancel_target = row
        classification = self.db.execute('SELECT priority_marker_required FROM jobs WHERE id=?', (marker,)).fetchone()
        if classification is None or classification[0] != 1:
            raise ControllerUnavailable('priority marker classification differs')
        if (state not in ('pending', 'registered', 'cancelled') or username is None or job_uid != uid or
                job_request != request_id or job_hash != request_hash):
            raise ControllerUnavailable('persisted marker and job differ')
        fence = self.db.execute('SELECT uid,username,account_generation,request_id,request_hash,effective_digest,state '
                                'FROM top_fence_intents WHERE marker=?', (marker,)).fetchone()
        expected_fence_state = 'closed' if state == 'cancelled' else 'linked'
        if digest_version != 1 or fence is None or tuple(fence) != (uid, username, generation, request_id, request_hash, digest, expected_fence_state):
            raise ControllerUnavailable('persisted marker has no matching fence intent')
        if state == 'cancelled':
            if job_state not in ('CANCELLED', 'EXPIRED') or scheduler_id is not None or cancel_target != job_state:
                raise ControllerUnavailable('cancelled marker and job state differ')
            return True

        # A retry may return the existing platform record without issuing
        # another scheduler or controller mutation. This is only a cached
        # lookup, not proof that the current controller state is reconciled.
        reported = {'SUBMITTING', 'PENDING', 'RUNNING', 'COMPLETING', 'SUSPENDED',
                    'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'NODE_FAIL',
                    'OUT_OF_MEMORY', 'BOOT_FAIL', 'PREEMPTED', 'DEADLINE', 'REVOKED'}
        if state == 'registered' and job_state in reported:
            origin = self.db.execute('SELECT origin FROM jobs WHERE id=?', (marker,)).fetchone()
            if (origin is None or origin[0] != 'platform' or not isinstance(digest, str) or
                    not re.fullmatch(r'[0-9a-f]{64}', digest) or
                    not self._verify_effective_job(uid, marker, digest)):
                raise ControllerUnavailable('recorded job identity cannot be verified')
            return True

        # Registration cannot prove a modified or already-submitted job is
        # still the exact accepted request. Other states need controller-side
        # identity reconciliation, not a fresh ADD3.
        if (job_state != 'WAITING_QUOTA' or scheduler_id is not None or
                cancel_target is not None or not self._verify_effective_job(uid, marker, digest)):
            raise ControllerUnavailable('marker job is no longer an unchanged waiting task')

        def recover(policy, status):
            current = self.db.execute('SELECT i.uid,j.owner_name,i.account_generation,i.request_id,'
                                      'i.request_hash,i.state,j.owner_uid,j.client_request_id,j.client_request_hash,'
                                      'j.effective_job_digest_version,j.effective_job_digest,j.state,j.scheduler_id,j.cancel_target '
                                      'FROM top_pending_intents i LEFT JOIN jobs j ON j.id=i.marker WHERE i.marker=?',
                                      (marker,)).fetchone()
            if current is None or tuple(current) != tuple(row):
                raise ControllerUnavailable('marker changed before recovery')
            if policy['enabled'] is not True or policy['identity_generation'] != generation:
                raise ControllerUnavailable('marker policy changed and requires reconciliation')
            self._same_controller()
            self.bridge.fence_top()
            status = self._refresh_after_controller_write(policy, status)
            current_job = self.db.execute('SELECT state,scheduler_id,cancel_target,effective_job_digest_version,'
                                          'effective_job_digest FROM jobs WHERE id=? AND owner_uid=?',
                                          (marker, uid)).fetchone()
            if (current_job is None or tuple(current_job) != ('WAITING_QUOTA', None, None, 1, digest) or
                    not self._verify_effective_job(uid, marker, digest)):
                raise ControllerUnavailable('marker job changed during recovery fence')
            receipt = self.bridge.register_top(marker, uid, generation, request_id, request_hash)
            if receipt != {'registered': True, 'identity': (marker, uid, generation, request_id, request_hash)}:
                raise ControllerOutcomeUnknown('marker recovery receipt differs')
            status = self._refresh_after_controller_write(policy, status)
            self._confirm_registered(marker, uid, username, generation, request_id, request_hash, digest)
            return True

        return self.runtime._with_verified_policy_locked(uid, username, recover)
