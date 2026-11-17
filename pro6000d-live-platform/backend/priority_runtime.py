"""Dependency seam for priority management; never constructed by production startup.

The readiness callback must independently verify current-boot peer/release,
controller recovery fence and protected anchor on every invocation. The lifecycle
provider must return a verified generation for the resolved Unix account.
"""
import logging
import re
from controller_bridge import ControllerUnavailable
from priority_admin_store import PriorityStateConflict

def _valid_controller_status(status):
    if not isinstance(status, dict):
        return False
    keys = set(status)
    if keys not in ({'epoch','boot','watermark'}, {'epoch','boot','watermark','ledger_seq','fenced'}):
        return False
    return ('fenced' not in status or (type(status['fenced']) is bool and
            type(status['ledger_seq']) is int and 0 <= status['ledger_seq'] <= 2**64 - 1))


class PriorityConflict(ValueError):
    pass


class PriorityRuntime:
    def __init__(self, resolve_user, lifecycle, store, coordinator, readiness, snapshot_proof=None):
        self.resolve_user, self.lifecycle = resolve_user, lifecycle
        self.store, self.coordinator, self.readiness = store, coordinator, readiness
        self.admission_guard = None
        self.promotion_handler = None
        self.cutover_ready = False
        # Must authenticate controller epoch, boot, watermark and chain together.
        # No production provider exists yet; absence keeps presentation disabled.
        self.snapshot_proof = snapshot_proof

    def reconciled_for_current_boot(self):
        try:
            return self.readiness() is True
        except Exception:
            return False

    def presentation(self, accounts):
        # Use the same ordering as writes for the shared SQLite connection.
        # A failed lock or reconciliation makes the feature unavailable.
        try:
            with self.coordinator.serialized():
                return self._presentation_locked(accounts)
        except Exception:
            logging.getLogger(__name__).exception('Priority presentation lock unavailable')
            return ({'available': False, 'reconciled': False}, {})

    def _presentation_locked(self, accounts):
        unavailable = ({'available': False, 'reconciled': False}, {})
        if not self.cutover_ready or self.snapshot_proof is None or not self.reconciled_for_current_boot():
            return unavailable
        transaction_started = False
        try:
            proof = self.snapshot_proof()
            if not isinstance(proof, dict) or type(proof.get('watermark')) is not int:
                return unavailable
            if self.store.db.in_transaction:
                return unavailable
            self.store.db.execute('BEGIN')
            transaction_started = True
            self.store.pending_confirmations()
            rows = self.store.db.execute('SELECT sequence,state FROM top_priority_outbox ORDER BY sequence').fetchall()
            committed = 0
            pending_seen = False
            for sequence, state in rows:
                if state == 'committed' and not pending_seen and sequence == committed + 1:
                    committed = sequence
                elif state == 'pending':
                    pending_seen = True
                else:
                    raise ValueError('priority history is not a committed prefix')
            if (proof['watermark'] != committed or
                self.coordinator.prefix_verifier(proof, committed, self.store.outbox) is not True):
                self.store.db.execute('ROLLBACK')
                transaction_started = False
                return unavailable
            result = {}
            for account in accounts:
                uid = account['uid']
                current = self.store.get(uid)
                if current is None:
                    continue
                user = self.resolve_user(uid)
                if (user.pw_uid != uid or user.pw_name != account['username'] or
                    current['uid'] != uid or current['username'] != user.pw_name or
                    self.lifecycle(user) != current['identity_generation']):
                    self.store.db.execute('ROLLBACK')
                    transaction_started = False
                    return unavailable
                result[uid] = {'top_priority': current['enabled'],
                               'top_priority_pending': current['pending_id'] is not None,
                               'top_priority_version': current['version'],
                               'top_priority_identity_generation': current['identity_generation']}
            self.store.db.execute('COMMIT')
            transaction_started = False
            if not self.reconciled_for_current_boot() or self.snapshot_proof() != proof:
                return unavailable
            return ({'available': True, 'reconciled': True}, result)
        except Exception:
            logging.getLogger(__name__).exception('Priority presentation could not be verified')
            return unavailable
        finally:
            if transaction_started:
                self.store.db.execute('ROLLBACK')

    def change(self, ident, payload):
        if not self.cutover_ready:
            raise ControllerUnavailable('priority generation cutover pending')
        # Serialize the policy decision and its durable intent with other
        # brokers' future marker/submission operations. Never nest flock.
        with self.coordinator.serialized():
            if not self.cutover_ready:
                raise ControllerUnavailable('priority generation cutover pending')
            if (self.promotion_handler is not None and isinstance(payload, dict) and
                    payload.get('enabled') is True and payload.get('expected_enabled') is False):
                return self.promotion_handler._change_or_promote_locked(ident, payload)
            return self._change_locked(ident, payload)

    def _fence_policy_locked(self):
        """Close the controller gate before sending a policy mutation."""
        transport = self.coordinator.transport
        # ORDERED_STATUS2 itself is available only after the controller fence
        # is closed. Fencing uses the separately attested controller PID.
        transport.fence_top()
        status = transport.status()
        if (not _valid_controller_status(status) or ('fenced' in status and status['fenced'] is not True) or
                status.get('epoch') != transport.epoch or not isinstance(status.get('boot'), str) or
                not re.fullmatch(r'[0-9a-f]{32}', status['boot']) or
                type(status.get('watermark')) is not int or status['watermark'] < 0 or
                not self.reconciled_for_current_boot() or transport.status() != status):
            raise ControllerUnavailable('priority controller status unavailable after fencing')

    def recover_policy(self, max_operations=32):
        """Reconcile the oldest durable policy intents before task recovery."""
        if type(max_operations) is not int or not 1 <= max_operations <= 256:
            raise ValueError('invalid policy recovery budget')
        with self.coordinator.serialized():
            if not self.reconciled_for_current_boot():
                raise ControllerUnavailable('priority prerequisites unavailable')
            count = 0
            while self.store.outbox.pending() is not None:
                if count >= max_operations:
                    raise ControllerUnavailable('priority policy recovery budget exhausted')
                self._fence_policy_locked()
                if self.coordinator._deliver_locked() is None:
                    raise ControllerUnavailable('priority policy recovery made no progress')
                count += 1
            # A committed outbox can still have an unconfirmed business state.
            # If neither exists, do not close a healthy open controller fence.
            if self.store.pending_confirmations():
                self._fence_policy_locked()
                self.coordinator._deliver_locked()
                if self.store.outbox.pending() is not None or self.store.pending_confirmations():
                    raise ControllerUnavailable('priority policy recovery remains incomplete')
            if not self.reconciled_for_current_boot():
                raise ControllerUnavailable('priority controller changed during recovery')
            return count

    def enroll_account(self, uid, username):
        """Create a disabled baseline in the ordered log, or verify an existing one."""
        import uuid
        if not self.cutover_ready:
            raise ControllerUnavailable('priority generation cutover pending')
        with self.coordinator.serialized():
            if not self.reconciled_for_current_boot():
                raise ControllerUnavailable('priority controller unavailable')
            while self.store.outbox.pending() is not None:
                if self.coordinator._deliver_locked() is None:
                    raise ControllerUnavailable('priority replay incomplete')
            user = self.resolve_user(uid)
            if user.pw_uid != uid or user.pw_name != username:
                raise ControllerUnavailable('Unix account changed')
            generation = self.lifecycle(user)
            existing = self.store.get(uid)
            if existing is not None:
                if existing['username'] != username or existing['identity_generation'] != generation:
                    raise ControllerUnavailable('existing account policy identity differs')
                return
            db = self.store.db
            db.execute('BEGIN IMMEDIATE')
            try:
                if self.store.get(uid) is not None:
                    raise ControllerUnavailable('account policy changed')
                self.store.outbox.enqueue_baseline('base-' + uuid.uuid4().hex, uid, username, generation)
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction: db.execute('ROLLBACK')
                raise
            self.coordinator._deliver_locked()
            confirmed = self.store.get(uid)
            if (confirmed is None or confirmed['username'] != username or
                    confirmed['identity_generation'] != generation):
                raise ControllerUnavailable('baseline not confirmed')

    def with_verified_policy(self, uid, username, operation):
        """Run a non-forking operation under the verified policy lock.

        The operation receives policy and the verified controller snapshot.
        It must persist any admission intent before releasing the lock. This
        check is not an admission authorization: the controller must atomically
        bind the same snapshot and identity to its barrier or otherwise fence
        concurrent changes and recover uncertain outcomes.
        """
        if type(uid) is not int or not isinstance(username, str) or not callable(operation):
            raise ValueError('invalid account or operation')
        self.enroll_account(uid, username)
        with self.coordinator.serialized():
            return self._with_verified_policy_locked(uid, username, operation)

    def _with_verified_policy_locked(self, uid, username, operation):
        """Caller must already hold the coordinator lock."""
        if not self.reconciled_for_current_boot():
            raise ControllerUnavailable('priority prerequisites unavailable')
        user = self.resolve_user(uid)
        generation = self.lifecycle(user)
        if user.pw_uid != uid or user.pw_name != username:
            raise ControllerUnavailable('account identity changed')
        db = self.store.db
        if db.in_transaction:
            raise ControllerUnavailable('priority database is busy')
        db.execute('BEGIN')
        try:
            policy = self.store.get(uid)
            pending = self.store.pending_confirmations()
            maximum = self.store.outbox.verify_sequence_integrity()
            rows = db.execute('SELECT sequence,state FROM top_priority_outbox ORDER BY sequence').fetchall()
            if (policy is None or policy['uid'] != uid or policy['username'] != username or
                policy['identity_generation'] != generation or policy['pending_id'] is not None or
                pending or maximum != len(rows) or
                any(seq != index or state != 'committed'
                    for index, (seq, state) in enumerate(rows, 1))):
                raise ControllerUnavailable('priority policy is not reconciled')
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        transport = self.coordinator.transport
        status = transport.status()
        if (not _valid_controller_status(status) or
            status.get('epoch') != transport.epoch or
            not isinstance(status.get('boot'), str) or not re.fullmatch(r'[0-9a-f]{32}', status['boot']) or
            type(status.get('watermark')) is not int or status['watermark'] != maximum or
            self.coordinator.prefix_verifier(status, maximum, self.store.outbox) is not True):
            raise ControllerUnavailable('priority controller history is not verified')
        expected = {'uid': uid, 'username': username, 'identity_generation': generation,
                    'enabled': policy['enabled'], 'version': policy['version']}
        if transport.user_status(status, uid, username, generation) != expected or transport.status() != status:
            raise ControllerUnavailable('priority controller policy changed')
        resolved = self.resolve_user(uid)
        if (resolved.pw_uid != uid or resolved.pw_name != username or
            self.lifecycle(resolved) != generation or not self.reconciled_for_current_boot()):
            raise ControllerUnavailable('account or controller identity changed')
        return operation(dict(policy), dict(status))

    def _change_locked(self, ident, payload, *, promotion_request=None):
        if not self.cutover_ready or not self.reconciled_for_current_boot():
            raise ControllerUnavailable('priority prerequisites unavailable')
        keys = {'uid', 'enabled', 'expected_enabled', 'expected_version', 'expected_identity_generation', 'request_id'}
        if not isinstance(payload, dict) or set(payload) != keys:
            raise ValueError('invalid priority fields')
        if (type(payload['enabled']) is not bool or type(payload['expected_enabled']) is not bool or
            payload['enabled'] == payload['expected_enabled'] or type(payload['expected_version']) is not int or
            not 0 <= payload['expected_version'] < 2**53-1 or
            not isinstance(payload['expected_identity_generation'], str) or
            not re.fullmatch(r'[0-9a-f]{32}', payload['expected_identity_generation']) or
            not isinstance(payload['request_id'], str) or not re.fullmatch(r'[A-Za-z0-9-]{8,80}', payload['request_id'])):
            raise ValueError('invalid priority fields')
        uid = payload['uid']
        if type(uid) is not int:
            raise ValueError('invalid uid')
        if self.admission_guard is not None and self.admission_guard(uid):
            if (promotion_request != payload['request_id'] or self.promotion_handler is None or
                    not self.promotion_handler._promotion_guard_locked(uid, promotion_request)):
                raise PriorityConflict('priority has unresolved admission intents')
        user = self.resolve_user(uid)
        if user.pw_uid != uid:
            raise ControllerUnavailable('resolved account uid does not match request')
        generation = self.lifecycle(user)
        if generation != payload['expected_identity_generation']:
            raise PriorityConflict('account generation changed')
        try:
            event = self.store.request_change(actor=ident, uid=user.pw_uid, username=user.pw_name,
            enabled=payload['enabled'], expected_enabled=payload['expected_enabled'],
            expected_version=payload['expected_version'], request_id=payload['request_id'],
            identity_generation=generation)
        except PriorityStateConflict as exc:
            raise PriorityConflict('priority state changed or request conflicts') from exc
        except Exception as exc:
            logging.getLogger(__name__).exception('Priority intent could not be safely persisted')
            raise ControllerUnavailable('priority ledger unavailable') from exc
        try:
            self._fence_policy_locked()
            self.coordinator._deliver_locked()
            if not self.reconciled_for_current_boot():
                return {'operation': {'request_id': event['request_id'], 'state': 'pending'}}
            self.store.pending_confirmations()
            current = self.store.get(uid)
            status = self.store.outbox.status(event['request_id'])
            committed = status['state'] == 'committed' and current['pending_id'] != event['request_id']
        except Exception:
            logging.getLogger(__name__).exception('Priority request %s remains pending after reconciliation failure', event['request_id'])
            return {'operation': {'request_id': event['request_id'], 'state': 'pending'}}
        return {'operation': {'request_id': event['request_id'], 'state': 'committed' if committed else 'pending'}}
