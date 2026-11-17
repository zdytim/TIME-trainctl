"""Fail-closed ordered delivery coordinator; not wired to the service.

The caller must supply an authenticated transport and a protected directory
containing a root-owned, private lock file shared by every broker process. The required prefix verifier must
authenticate the controller's committed history against an independent durable
anchor; matching sequence numbers alone do not prove the histories are equal.
Controller identity, account provisioning and activation remain external.

Not safe to activate until every submit and policy path uses this lock and the
controller enforces the recovered barrier. Provisioning must prevent root from
replacing the lock inode while brokers are running.
Code inside the lock must not fork. A fork through Python's registered fork
hooks from the locking thread, or while a lock descriptor is being opened or
closed, terminates the child before it can perform work. Native fork,
subprocess and callback behavior must be reviewed separately; these hooks do
not prove their safety.
"""
import fcntl
import os
import stat
import threading
from contextlib import contextmanager

from controller_bridge import ControllerUnavailable


_process_lock = threading.RLock()
_lock_state = threading.local()
_active_fds = set()
_global_poisoned = False
_fd_transition = False


def _reset_after_fork():
    # Drop inherited open-file descriptions without unlocking the parent's
    # lock. This also avoids retaining a lock if the parent later crashes.
    global _process_lock, _lock_state, _active_fds
    forked_from_critical_section = (getattr(_lock_state, 'held', False) or
                                    getattr(_lock_state, 'entering', False))
    if _global_poisoned or _fd_transition:
        os._exit(70)
    for fd in _active_fds:
        try:
            os.close(fd)
        except OSError:
            pass
    _active_fds = set()
    if forked_from_critical_section:
        os._exit(70)
    _process_lock = threading.RLock()
    _lock_state = threading.local()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_reset_after_fork)


def _open_lock(directory):
    global _global_poisoned
    if not isinstance(directory, str) or not directory.startswith('/'):
        raise ValueError('absolute protected lock directory required')
    parts = directory[1:].split('/')
    if any(not part or part in ('.', '..') or len(os.fsencode(part)) > 255 for part in parts):
        raise ValueError('invalid lock directory')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current = os.open('/', flags)
    fd = None
    try:
        for part in [None, *parts]:
            if part is not None:
                next_fd = os.open(part, flags, dir_fd=current)
                previous, current = current, None
                try:
                    os.close(previous)
                except BaseException:
                    _global_poisoned = True
                    os.close(next_fd)
                    raise
                current = next_fd
            info = os.fstat(current)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError('untrusted lock directory')
        fd = os.open('priority-delivery.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=current)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
                    stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise ValueError('untrusted priority lock')
            try:
                os.close(current)
            except BaseException:
                _global_poisoned = True
                current = None
                raise
            current = None
            result, fd = fd, None
            return result
        except BaseException:
            if fd is not None:
                try:
                    os.close(fd)
                except BaseException:
                    _global_poisoned = True
                    raise
            raise
    finally:
        if current is not None:
            try:
                os.close(current)
            except BaseException:
                _global_poisoned = True
                raise


class PriorityCoordinator:
    def __init__(self, store, transport, lock_directory, prefix_verifier, anchor_advancer):
        if store.outbox.db is not store.db or not isinstance(lock_directory, str) or not lock_directory.startswith('/'):
            raise ValueError('shared store and absolute lock directory required')
        if not callable(prefix_verifier):
            raise ValueError('authenticated history prefix verifier required')
        if not callable(anchor_advancer):
            raise ValueError('durable authenticated anchor advancer required')
        self.store, self.outbox, self.transport = store, store.outbox, transport
        self.lock_directory = lock_directory
        self.prefix_verifier = prefix_verifier
        self.anchor_advancer = anchor_advancer
        self._pid = os.getpid()
        self._poisoned = False

    @contextmanager
    def serialized(self):
        """Take the shared broker lock once, before any SQLite transaction.

        Callers must not re-enter this lock or start with an open transaction.
        All brokers must use the same trusted lock inode.
        """
        global _global_poisoned, _fd_transition
        if getattr(_lock_state, 'held', False) or getattr(_lock_state, 'entering', False):
            raise RuntimeError('priority coordinator lock is not reentrant')
        if os.getpid() != self._pid:
            raise RuntimeError('priority coordinator cannot be inherited across fork')
        with _process_lock:
            if self._poisoned or _global_poisoned:
                raise RuntimeError('priority coordinator lock is unusable')
            if self.store.db.in_transaction:
                raise ValueError('priority lock requires an idle database')
            owner_pid = os.getpid()
            _lock_state.entering = True
            _fd_transition = True
            try:
                fd = _open_lock(self.lock_directory)
                try:
                    _active_fds.add(fd)
                except BaseException:
                    try:
                        os.close(fd)
                    except BaseException:
                        self._poisoned = True
                        _global_poisoned = True
                        raise
                    try:
                        _active_fds.discard(fd)
                    except BaseException:
                        self._poisoned = True
                        _global_poisoned = True
                        raise
                    raise
            except BaseException:
                _lock_state.entering = False
                raise
            finally:
                _fd_transition = False
            try:
                # A live peer or orphaned lock must fail closed instead of
                # indefinitely blocking the process-wide coordination mutex.
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _lock_state.held = True
                try:
                    yield
                finally:
                    if os.getpid() != owner_pid:
                        raise RuntimeError('priority lock context cannot continue after fork')
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except BaseException:
                        self._poisoned = True
                        raise
                    finally:
                        _lock_state.held = False
            finally:
                if os.getpid() == owner_pid:
                    _fd_transition = True
                    try:
                        os.close(fd)
                    except BaseException:
                        self._poisoned = True
                        _global_poisoned = True
                        raise
                    else:
                        try:
                            _active_fds.discard(fd)
                        except BaseException:
                            self._poisoned = True
                            _global_poisoned = True
                            raise
                    finally:
                        _fd_transition = False
                        _lock_state.entering = False

    def deliver_next(self):
        """Recover local confirmations, then deliver at most one intent.

        A status watermark is used only to reject inconsistent histories; it
        never proves a particular request was committed. A reply can be lost
        after the controller writes, so the exact pending identity is retried.
        """
        with self.serialized():
            return self._deliver_locked()

    def assert_serialized(self):
        if os.getpid() != self._pid or not getattr(_lock_state, 'held', False):
            raise RuntimeError('priority coordinator lock is required')

    def _deliver_locked(self):
        if self.store.db.in_transaction:
            raise ValueError('coordinator requires an idle database')
        # Capture the local prefix in one read snapshot: a concurrent enqueue
        # must not make sqlite_sequence appear ahead of the rows we inspected.
        self.store.db.execute('BEGIN')
        try:
            self.outbox.verify_sequence_integrity()
            pending_ids = self.store.pending_confirmations()
            rows = self.store.db.execute('SELECT sequence,state FROM top_priority_outbox ORDER BY sequence').fetchall()
            event = self.outbox.pending()
            self.store.db.execute('COMMIT')
        except BaseException:
            if self.store.db.in_transaction:
                self.store.db.execute('ROLLBACK')
            raise
        committed = 0
        seen_pending = False
        for sequence, state in rows:
            if state == 'committed' and not seen_pending:
                committed = sequence
            elif state == 'pending':
                seen_pending = True
            else:
                raise ValueError('priority outbox is not a committed prefix')
        status = self.transport.status()
        if not isinstance(status, dict) or status.get('epoch') != self.transport.epoch or type(status.get('watermark')) is not int:
            raise ControllerUnavailable('controller identity or watermark is not verified')
        allowed = {committed}
        if event is not None:
            if event['sequence'] != committed + 1:
                raise ValueError('oldest priority operation is not next')
            allowed.add(event['sequence'])
        if status['watermark'] not in allowed:
            raise ControllerUnavailable('controller and broker histories do not have a reconcilable prefix')
        if self.prefix_verifier(status, committed, self.outbox) is not True:
            raise ControllerUnavailable('controller history prefix is not authenticated')
        # Only after authenticated remote reconciliation may a previously
        # recorded controller result become a visible user confirmation.
        for request_id in pending_ids:
            existing = self.outbox.status(request_id)
            if existing['state'] == 'committed':
                self.store.apply_confirmation(request_id, self.transport.epoch)
        if event is None:
            return None

        if event['payload'].get('action') == 'baseline_top_priority':
            from ordered_anchor_reader import read_anchor, append_anchor
            from priority_baseline_chain import baseline_chain_step
            from priority_chain import ControllerAnchorLinkVerifier
            import hashlib, json
            payload = event['payload']
            directory = getattr(self.anchor_advancer, 'directory', None)
            if not directory:
                raise ControllerUnavailable('baseline anchor unavailable')
            self.transport.fence_top()
            self.transport.apply_baseline(event['sequence'], event['request_id'], payload['uid'],
                                          payload['username'], payload['identity_generation'])
            current = self.transport.status()
            if current.get('epoch') != status['epoch'] or current.get('boot') != status['boot'] or current.get('watermark') != event['sequence']:
                raise ControllerUnavailable('baseline controller changed')
            last = read_anchor(directory)
            if last['sequence'] == event['sequence'] - 1:
                digest = baseline_chain_step(last['hash'], current['epoch'], event['sequence'], event['request_id'],
                                             payload['uid'], payload['username'], payload['identity_generation'])
            elif last['sequence'] == event['sequence']:
                previous = self.transport.chain_lookup(current, event['sequence'] - 1)
                digest = baseline_chain_step(previous, current['epoch'], event['sequence'], event['request_id'],
                                             payload['uid'], payload['username'], payload['identity_generation'])
            else:
                raise ControllerUnavailable('baseline anchor sequence differs')
            if self.transport.chain_lookup(current, event['sequence']) != digest:
                raise ControllerUnavailable('baseline chain differs')
            policy = self.transport.user_status(current, payload['uid'], payload['username'], payload['identity_generation'])
            if policy != {'uid': payload['uid'], 'username': payload['username'],
                          'identity_generation': payload['identity_generation'], 'enabled': False, 'version': 0}:
                raise ControllerUnavailable('baseline policy differs')
            append_anchor(directory, current['epoch'], event['sequence'], digest, ControllerAnchorLinkVerifier(self.transport))
            db = self.store.db
            db.execute('BEGIN IMMEDIATE')
            try:
                if self.outbox.pending() != event or self.transport.status() != current:
                    raise ControllerUnavailable('baseline changed during confirmation')
                user = db.execute('SELECT uid FROM top_priority_users WHERE uid=? OR username=?',
                                  (payload['uid'], payload['username'])).fetchone()
                if user is not None:
                    raise ControllerUnavailable('baseline would overwrite policy')
                db.execute('INSERT INTO top_priority_users VALUES(?,?,0,0,NULL,?)',
                           (payload['uid'],payload['username'],payload['identity_generation']))
                db.execute('INSERT INTO top_priority_baselines VALUES(?,?,?,0,0,?,?)',
                           (payload['uid'],payload['username'],payload['identity_generation'],current['epoch'],event['sequence']))
                result = json.dumps({'baseline': True},sort_keys=True,separators=(',', ':'))
                if db.execute("UPDATE top_priority_outbox SET state='committed',result=?,result_digest=? WHERE sequence=? AND state='pending'",
                              (result,hashlib.sha256(result.encode()).hexdigest(),event['sequence'])).rowcount != 1:
                    raise ControllerUnavailable('baseline intent changed')
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction: db.execute('ROLLBACK')
                raise
            return event

        def exchange(message):
            result = self.transport.apply(message['sequence'], message['request_id'], message['digest'],
                                          message['payload'], expected_status=status)
            # A controller receipt alone is not enough: persist its verified
            # chain anchor before the outbox can become committed locally.
            from priority_protocol import verify_controller_result
            verify_controller_result(message, result, self.transport.epoch)
            if self.anchor_advancer(message, result) is not True:
                raise ControllerUnavailable('controller result was not durably anchored')
            reply = {key: message[key] for key in ('sequence', 'request_id', 'digest')}
            reply.update({'status': 'committed', 'result': result})
            return reply

        sent = self.outbox.deliver_one(exchange, self.transport.epoch)
        self.store.apply_confirmation(sent['request_id'], self.transport.epoch)
        return sent
