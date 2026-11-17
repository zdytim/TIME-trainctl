"""Durable ordering for future controller-backed priority mutations.

This component does not enable priority or grant quota.  The controller peer
must durably serialize sequence numbers and reject stale, conflicting or
out-of-order mutations; a local outbox alone cannot enforce that invariant.
"""
import hashlib
import json
import re
from priority_protocol import request_audit_detail, validate_epoch, validate_priority_payload, verify_controller_result


class PriorityOutbox:
    def __init__(self, db):
        if db.isolation_level is not None or db.in_transaction:
            raise ValueError('priority outbox requires an idle autocommit connection')
        self.db = db
        self.db.execute("""CREATE TABLE IF NOT EXISTS top_priority_outbox (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL,
            digest TEXT NOT NULL,
            target_uid INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending','committed')),
            result TEXT, result_digest TEXT
        )""")
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(top_priority_outbox)')}
        if not {'sequence','request_id','payload','digest','target_uid','state','result','result_digest'} <= columns:
            raise ValueError('priority outbox schema requires reviewed migration')
        self.db.execute('CREATE INDEX IF NOT EXISTS top_priority_outbox_uid_seq ON top_priority_outbox(target_uid,sequence)')
        self.db.execute('CREATE TABLE IF NOT EXISTS priority_baseline_audit '
                        '(request_id TEXT PRIMARY KEY, uid INTEGER NOT NULL, username TEXT NOT NULL, '
                        'generation TEXT NOT NULL, digest TEXT NOT NULL)')

    @staticmethod
    def _event(row):
        if row is None:
            return None
        sequence, request_id, payload, digest, target_uid = row
        if not isinstance(payload, str) or not isinstance(digest, str) or hashlib.sha256(payload.encode('utf-8')).hexdigest() != digest:
            raise ValueError('persisted priority mutation failed integrity check')
        decoded = json.loads(payload)
        if not isinstance(decoded, dict) or json.dumps(decoded, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False) != payload:
            raise ValueError('persisted priority mutation is not canonical JSON')
        PriorityOutbox._validate_event_payload(decoded)
        if type(target_uid) is not int or target_uid != decoded['uid']:
            raise ValueError('persisted priority target differs from payload')
        return {'sequence': sequence, 'request_id': request_id,
                'payload': decoded, 'digest': digest}

    @staticmethod
    def _validate_event_payload(payload):
        if isinstance(payload, dict) and payload.get('action') == 'baseline_top_priority':
            if (set(payload) != {'action','uid','username','identity_generation'} or
                    type(payload['uid']) is not int or not 0 <= payload['uid'] <= 0xffffffff or
                    not isinstance(payload['username'], str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', payload['username']) or
                    not isinstance(payload['identity_generation'], str) or not re.fullmatch(r'[0-9a-f]{32}', payload['identity_generation'])):
                raise ValueError('invalid baseline identity')
            return
        validate_priority_payload(payload)

    def enqueue_baseline(self, request_id, uid, username, generation):
        """Reserve a shared global sequence inside the caller's transaction."""
        if not self.db.in_transaction:
            raise ValueError('baseline requires a durable business transaction')
        payload = {'action': 'baseline_top_priority', 'uid': uid, 'username': username,
                   'identity_generation': generation}
        self._validate_event_payload(payload)
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9-]{8,80}', request_id):
            raise ValueError('invalid baseline request')
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        digest = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        row = self.db.execute('SELECT sequence,request_id,payload,digest,target_uid FROM top_priority_outbox WHERE request_id=?', (request_id,)).fetchone()
        if row is None:
            self.db.execute("INSERT INTO top_priority_outbox(request_id,payload,digest,target_uid,state) VALUES(?,?,?,?, 'pending')", (request_id, encoded, digest, uid))
            row = self.db.execute('SELECT sequence,request_id,payload,digest,target_uid FROM top_priority_outbox WHERE request_id=?', (request_id,)).fetchone()
        if row[2] != encoded or row[3] != digest or row[4] != uid:
            raise ValueError('conflicting baseline request')
        audit = self.db.execute('SELECT uid,username,generation,digest FROM priority_baseline_audit WHERE request_id=?', (request_id,)).fetchone()
        expected = (uid, username, generation, digest)
        if audit is None:
            self.db.execute('INSERT INTO priority_baseline_audit VALUES(?,?,?,?,?)', (request_id, *expected))
        elif tuple(audit) != expected:
            raise ValueError('baseline audit conflict')
        return self._event(row)

    def enqueue(self, request_id, payload):
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9-]{8,80}', request_id):
            raise ValueError('invalid request id')
        validate_priority_payload(payload)
        try:
            encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                 ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise ValueError('payload is not canonical JSON') from exc
        digest = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        # Business state, authorization evidence, audit and this intent must
        # share the caller's transaction. Never send an uncommitted intent.
        if not self.db.in_transaction:
            raise ValueError('priority intent requires the caller business transaction')
        row = self.db.execute('SELECT sequence, request_id, payload, digest, target_uid FROM top_priority_outbox WHERE request_id=?', (request_id,)).fetchone()
        if row is not None:
            if row[2] != encoded or row[3] != digest:
                raise ValueError('request id already belongs to a different mutation')
        else:
            self.db.execute('INSERT INTO top_priority_outbox(request_id,payload,digest,target_uid,state) VALUES(?,?,?,?,?)',
                            (request_id, encoded, digest, payload['uid'], 'pending'))
            row = self.db.execute('SELECT sequence, request_id, payload, digest, target_uid FROM top_priority_outbox WHERE request_id=?', (request_id,)).fetchone()
        return self._event(row)

    def pending(self):
        row = self.db.execute("SELECT sequence, request_id, payload, digest, target_uid FROM top_priority_outbox WHERE state='pending' ORDER BY sequence LIMIT 1").fetchone()
        return self._event(row)

    def verify_sequence_integrity(self):
        """Fail closed on local sequence gaps; controller watermark is separate."""
        count, minimum, maximum = self.db.execute('SELECT COUNT(*),COALESCE(MIN(sequence),0),COALESCE(MAX(sequence),0) FROM top_priority_outbox').fetchone()
        row = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='top_priority_outbox'").fetchone()
        allocated = row[0] if row else 0
        if maximum != allocated or count != maximum or (count and minimum != 1):
            raise ValueError('priority outbox sequence has a gap or inconsistent watermark')
        return maximum

    def status(self, request_id):
        row = self.db.execute('SELECT sequence, request_id, payload, digest, target_uid, state, result, result_digest FROM top_priority_outbox WHERE request_id=?', (request_id,)).fetchone()
        if row is None:
            return None
        event = self._event(row[:5])
        result = None
        if row[6] is not None or row[7] is not None:
            if not isinstance(row[6], str) or not isinstance(row[7], str) or hashlib.sha256(row[6].encode('utf-8')).hexdigest() != row[7]:
                raise ValueError('persisted priority result failed integrity check')
            result = json.loads(row[6])
            if not isinstance(result, dict) or json.dumps(result,sort_keys=True,separators=(',', ':'),ensure_ascii=False,allow_nan=False) != row[6]:
                raise ValueError('persisted priority result is not canonical JSON')
        return {**event, 'state': row[5], 'result': result}

    def _verify_business_binding(self, event, expected_epoch=None, check_history=True):
        p = event['payload']
        own_transaction = not self.db.in_transaction
        if own_transaction:
            self.db.execute('BEGIN')
        try:
            if check_history:
                # Avoid a second connection and keep history plus binding in
                # one SQLite snapshot. No schema changes occur in this view.
                self.verify_sequence_integrity()
                from priority_admin_store import PriorityAdminStore
                view = object.__new__(PriorityAdminStore)
                view.db,view.outbox = self.db,self
                view.pending_confirmations(uid=p['uid'])
            rows = self.db.execute('''SELECT u.uid,b.controller_epoch,b.controller_watermark FROM top_priority_users u
                JOIN top_priority_baselines b ON b.uid=u.uid AND b.username=u.username AND b.identity_generation=u.identity_generation
                JOIN audit_log a ON a.object_id=u.pending_id AND a.object_type='priority_user'
                WHERE u.uid=? AND u.username=? AND u.identity_generation=? AND u.enabled=? AND u.version=?
                  AND u.pending_id=? AND a.actor_uid=? AND a.actor_name=? AND a.action='priority_request'
                  AND a.result='pending' AND a.detail_json=?''',
                (p['uid'],p['username'],p['identity_generation'],int(p['expected_enabled']),p['expected_version'],
                 event['request_id'],p['actor_uid'],p['actor_name'],request_audit_detail(event))).fetchall()
            if len(rows) != 1 or type(rows[0][2]) is not int or event['sequence'] <= rows[0][2]:
                raise ValueError('priority intent, baseline, business state and audit do not match')
            validate_epoch(rows[0][1])
            if expected_epoch is not None and rows[0][1] != expected_epoch:
                raise ValueError('authenticated controller epoch differs from priority baseline')
            if own_transaction:
                self.db.execute('COMMIT')
        except BaseException as exc:
            if own_transaction and self.db.in_transaction:
                self.db.execute('ROLLBACK')
            if isinstance(exc,Exception):
                raise ValueError('priority business state cannot be verified') from exc
            raise

    def _audit_count(self, event, action, result):
        p = event['payload']
        if p.get('action') == 'baseline_top_priority':
            if (action, result) != ('priority_request', 'pending'):
                return 0
            return self.db.execute('SELECT COUNT(*) FROM priority_baseline_audit WHERE request_id=? AND uid=? AND username=? AND generation=? AND digest=?',
                                   (event['request_id'], p['uid'], p['username'], p['identity_generation'], event['digest'])).fetchone()[0]
        return self.db.execute('''SELECT COUNT(*) FROM audit_log WHERE actor_uid=? AND actor_name=? AND action=?
            AND object_type='priority_user' AND object_id=? AND detail_json=? AND result=?''',
            (p['actor_uid'],p['actor_name'],action,event['request_id'],request_audit_detail(event),result)).fetchone()[0]

    def deliver_one(self, exchange, expected_epoch=None):
        """Send only the oldest intent; unknown or conflicting replies stay pending.

        Duplicate delivery is expected, including after a lost reply. The
        controller must identify a request by sequence, id and payload digest
        and must fence delayed duplicates after later operations.
        """
        if self.db.in_transaction:
            raise ValueError('priority outbox cannot deliver inside another transaction')
        event = self.pending()
        if event is None:
            return None
        # The caller must have authenticated this epoch on the connection used
        # by exchange. Validate before sending any privileged mutation.
        validate_epoch(expected_epoch)
        self._verify_business_binding(event,expected_epoch)
        # The transport must not be able to mutate the identity we check.
        message = json.loads(json.dumps(event, ensure_ascii=False, allow_nan=False))
        reply = exchange(message)
        if not isinstance(reply, dict) or type(reply.get('sequence')) is not int or reply.get('status') != 'committed' or any(
                reply.get(key) != event[key] for key in ('sequence', 'request_id', 'digest')):
            raise ValueError('controller did not confirm the exact mutation')
        result = reply.get('result')
        if not isinstance(result, dict):
            raise ValueError('controller did not provide the resulting priority state')
        if result is not None and not isinstance(result, dict):
            raise ValueError('invalid controller result')
        verify_controller_result(event,result,expected_epoch)
        encoded = json.dumps(result,sort_keys=True,separators=(',', ':'),ensure_ascii=False,allow_nan=False) if result is not None else None
        result_digest = hashlib.sha256(encoded.encode('utf-8')).hexdigest() if encoded is not None else None
        expected_payload = json.dumps(event['payload'],sort_keys=True,separators=(',', ':'),ensure_ascii=False,allow_nan=False)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT state,request_id,digest,result,result_digest,target_uid,payload FROM top_priority_outbox WHERE sequence=?', (event['sequence'],)).fetchone()
            if row is None:
                raise ValueError('priority mutation disappeared before acknowledgement')
            if row[1] != event['request_id'] or row[2] != event['digest'] or row[5] != event['payload']['uid'] or row[6] != expected_payload:
                raise ValueError('priority mutation identity changed before acknowledgement')
            if row[0] == 'pending':
                self._verify_business_binding(event,expected_epoch)
                changed = self.db.execute("UPDATE top_priority_outbox SET state='committed',result=?,result_digest=? WHERE sequence=? AND request_id=? AND digest=? AND target_uid=? AND payload=? AND state='pending'",
                                          (encoded,result_digest,event['sequence'],event['request_id'],event['digest'],event['payload']['uid'],expected_payload)).rowcount
                if changed != 1:
                    raise ValueError('priority mutation changed before acknowledgement')
            elif tuple(row) != ('committed',event['request_id'],event['digest'],encoded,result_digest,event['payload']['uid'],expected_payload):
                raise ValueError('priority mutation changed before acknowledgement')
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise
        return event
