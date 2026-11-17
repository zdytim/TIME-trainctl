"""Transactional administrator intent storage; not an enabled policy service.

The caller must authenticate the administrator, resolve the target's Unix
identity, and verify the controller's identity, epoch and durable watermark.
This store must not be exposed until the matching controller protocol and
recovery fence are integrated.  A SQLite transaction is not a scheduler gate.
"""
import re
import datetime as dt
from priority_protocol import request_audit_detail, validate_epoch, verify_controller_result


class PriorityStateConflict(ValueError):
    """A valid request no longer matches the expected user state."""


class PriorityAdminStore:
    def __init__(self, db, outbox):
        if db.isolation_level is not None or db.in_transaction or outbox.db is not db:
            raise ValueError('an idle shared autocommit database is required')
        self.db, self.outbox = db, outbox
        db.execute("""CREATE TABLE IF NOT EXISTS top_priority_users (
            uid INTEGER PRIMARY KEY, username TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
            version INTEGER NOT NULL CHECK(version >= 0), pending_id TEXT UNIQUE,
            identity_generation TEXT NOT NULL
        )""")
        columns = {row[1] for row in db.execute('PRAGMA table_info(top_priority_users)')}
        if not {'uid','username','enabled','version','pending_id','identity_generation'} <= columns:
            raise ValueError('priority identity schema requires reviewed migration')
        db.execute('''CREATE TABLE IF NOT EXISTS top_priority_baselines (
            uid INTEGER PRIMARY KEY, username TEXT NOT NULL, identity_generation TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), version INTEGER NOT NULL CHECK(version>=0),
            controller_epoch TEXT NOT NULL, controller_watermark INTEGER NOT NULL CHECK(controller_watermark>=0)
        )''')
        db.execute('CREATE INDEX IF NOT EXISTS priority_audit_lookup ON audit_log(object_type,object_id,action,result)')

    @staticmethod
    def _uid(value):
        if type(value) is not int or not 0 <= value <= 0xffffffff:
            raise ValueError('invalid Unix uid')
        return value

    @staticmethod
    def _username(value):
        if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('invalid Unix username')
        return value

    @staticmethod
    def _version(value):
        if type(value) is not int or not 0 <= value <= 2**53-1:
            raise ValueError('invalid priority version')
        return value

    @staticmethod
    def _generation(value):
        if not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{32}',value):
            raise ValueError('account generation is not verified')
        return value

    def get(self, uid):
        self._uid(uid)
        row = self.db.execute('SELECT uid,username,enabled,version,pending_id,identity_generation FROM top_priority_users WHERE uid=?', (uid,)).fetchone()
        if row is None:
            return None
        uid, username, enabled, version, pending_id, identity_generation = row
        return {'uid': uid, 'username': username, 'enabled': bool(enabled), 'version': version,
                'state': 'pending' if pending_id is not None else 'confirmed', 'pending_id': pending_id,
                'identity_generation':self._generation(identity_generation)}

    def install_confirmed_snapshot(self, uid, username, enabled, version, identity_generation, controller_epoch, controller_watermark):
        """Seed only a verified controller snapshot; never overwrite divergence.

        Controller verification is the caller's responsibility. A conflicting
        existing record or unresolved operation must be reconciled elsewhere.
        """
        self._uid(uid); self._username(username); self._version(version); self._generation(identity_generation)
        validate_epoch(controller_epoch)
        if type(controller_watermark) is not int or not 0 <= controller_watermark < 2**63:
            raise ValueError('controller watermark is not verified')
        if type(enabled) is not bool:
            raise ValueError('enabled must be boolean')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            current = self.get(uid)
            baseline = self.db.execute('SELECT username,identity_generation,enabled,version,controller_epoch,controller_watermark FROM top_priority_baselines WHERE uid=?',(uid,)).fetchone()
            if current is None and baseline is None:
                maximum = self.outbox.verify_sequence_integrity()
                pending = self.pending_confirmations()
                epochs = {row[0] for row in self.db.execute('SELECT DISTINCT controller_epoch FROM top_priority_baselines')}
                if pending or maximum != controller_watermark or (epochs and epochs != {controller_epoch}):
                    raise ValueError('controller watermark or epoch requires explicit reconciliation')
                self.db.execute('INSERT INTO top_priority_users VALUES (?,?,?,?,NULL,?)', (uid, username, int(enabled), version, identity_generation))
                self.db.execute('INSERT INTO top_priority_baselines VALUES (?,?,?,?,?,?,?)',(uid,username,identity_generation,int(enabled),version,controller_epoch,controller_watermark))
            elif (current is None or baseline is None or tuple(baseline)!=(username,identity_generation,int(enabled),version,controller_epoch,controller_watermark) or
                  (current['username'], current['enabled'], current['version'], current['state'], current['identity_generation']) != (username, enabled, version, 'confirmed', identity_generation)):
                raise ValueError('stored state differs from controller snapshot')
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def request_change(self, *, actor, uid, username, enabled, expected_enabled, expected_version, request_id, identity_generation):
        # actor must come from the broker's authenticated Unix peer, never the
        # API request body; repeat the administrator check at this boundary.
        if not isinstance(actor,dict) or actor.get('is_admin') is not True:
            raise PermissionError('administrator identity required')
        actor_uid = self._uid(actor.get('uid'))
        actor_name = self._username(actor.get('username'))
        self._uid(uid); self._username(username); self._version(expected_version); self._generation(identity_generation)
        if expected_version == 2**53-1:
            raise ValueError('priority version cannot advance beyond safe integer range')
        if type(enabled) is not bool or type(expected_enabled) is not bool or enabled == expected_enabled:
            raise ValueError('priority state change must contain distinct booleans')
        payload = {'action':'set_top_priority', 'actor_uid':actor_uid, 'actor_name':actor_name, 'uid':uid, 'username':username,
                   'enabled':enabled, 'expected_enabled':expected_enabled, 'expected_version':expected_version,
                   'identity_generation':identity_generation}
        self.db.execute('BEGIN IMMEDIATE')
        try:
            previous = self.outbox.status(request_id)
            self.pending_confirmations(uid=uid)
            if previous is not None:
                if previous['payload'] != payload:
                    raise PriorityStateConflict('request id belongs to a different change')
                self.db.execute('COMMIT')
                return {k:previous[k] for k in ('sequence','request_id','payload','digest')}
            current = self.get(uid)
            if current is None:
                raise ValueError('priority account baseline is unavailable')
            if current['username'] != username or current['identity_generation'] != identity_generation or current['state'] != 'confirmed' or current['enabled'] != expected_enabled or current['version'] != expected_version:
                raise PriorityStateConflict('priority state changed or is not reconciled')
            event = self.outbox.enqueue(request_id, payload)
            changed = self.db.execute('UPDATE top_priority_users SET pending_id=? WHERE uid=? AND username=? AND pending_id IS NULL AND enabled=? AND version=?',
                                      (request_id, uid, username, int(expected_enabled), expected_version)).rowcount
            if changed != 1:
                raise PriorityStateConflict('priority state changed concurrently')
            self._audit(event,'priority_request','pending')
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise
        return event

    def _audit(self, event, action, result):
        p = event['payload']
        detail = request_audit_detail(event)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
        self.db.execute('''INSERT INTO audit_log(actor_uid,actor_name,action,object_type,object_id,detail_json,result,created_at)
            VALUES(?,?,?,?,?,?,?,?)''',(p['actor_uid'],p['actor_name'],action,'priority_user',event['request_id'],detail,result,now))

    def pending_confirmations(self, uid=None):
        """Check both directions in one snapshot; return unresolved IDs in order.

        This local check does not prove controller identity or its watermark.
        """
        own_transaction = not self.db.in_transaction
        filter_uid = None if uid is None else self._uid(uid)
        if own_transaction:
            self.db.execute('BEGIN')
        try:
            if filter_uid is None:
                self.outbox.verify_sequence_integrity()
            where = '' if filter_uid is None else ' WHERE uid=?'
            args = () if filter_uid is None else (filter_uid,)
            baselines = self.db.execute('SELECT uid,username,identity_generation,enabled,version,controller_epoch,controller_watermark FROM top_priority_baselines'+where,args).fetchall()
            if filter_uid is None and len({row[5] for row in baselines}) > 1:
                raise ValueError('priority baselines belong to different controller epochs')
            users = self.db.execute('SELECT uid FROM top_priority_users'+where,args).fetchall()
            if {row[0] for row in baselines} != {row[0] for row in users}:
                raise ValueError('priority users and verified baselines differ')
            latest = {}
            for base_uid,username,generation,enabled,version,epoch,watermark in baselines:
                self._uid(base_uid); self._username(username); self._generation(generation); self._version(version); validate_epoch(epoch)
                if enabled not in (0,1) or type(watermark) is not int or not 0 <= watermark < 2**63:
                    raise ValueError('priority baseline is invalid')
                latest[base_uid] = {'username':username,'generation':generation,'enabled':bool(enabled),
                                    'version':version,'pending':False,'request_id':None,
                                    'epoch':epoch,'watermark':watermark}
            pending_filter = '' if filter_uid is None else ' AND u.uid=?'
            rows = self.db.execute('''SELECT u.uid,u.username,u.identity_generation,u.enabled,u.version,u.pending_id,o.sequence
                FROM top_priority_users u LEFT JOIN top_priority_outbox o ON o.request_id=u.pending_id
                WHERE u.pending_id IS NOT NULL'''+pending_filter+' ORDER BY o.sequence',args).fetchall()
            pending = []
            for uid,username,generation,enabled,version,request_id,sequence in rows:
                event = self.outbox.status(request_id)
                if sequence is None or event is None or event['sequence'] != sequence:
                    raise ValueError('priority state has an orphaned pending request')
                p = event['payload']
                if (p['uid'],p['username'],p['identity_generation'],p['expected_enabled'],p['expected_version']) != (uid,username,generation,bool(enabled),version):
                    raise ValueError('pending request conflicts with stored priority state')
                self.outbox._verify_business_binding(event,check_history=False)
                pending.append(request_id)
            pending_set = set(pending)
            outbox_filter = '' if filter_uid is None else ' WHERE target_uid=?'
            for (request_id,) in self.db.execute('SELECT request_id FROM top_priority_outbox'+outbox_filter+' ORDER BY sequence',args):
                event = self.outbox.status(request_id)
                if self.outbox._audit_count(event,'priority_request','pending') != 1:
                    raise ValueError('priority intent lacks its unique request audit')
                p = event['payload']
                prior = latest.get(p['uid'])
                if p.get('action') == 'baseline_top_priority':
                    generation = self.db.execute('SELECT username,generation FROM unix_account_generations WHERE uid=?', (p['uid'],)).fetchone()
                    if generation is None or tuple(generation) != (p['username'], p['identity_generation']):
                        raise ValueError('baseline account generation differs')
                    if event['state'] == 'pending':
                        if prior is not None or event['result'] is not None:
                            raise ValueError('pending baseline conflicts with policy')
                    elif (event['state'] != 'committed' or event['result'] != {'baseline': True} or prior is None or
                          prior['username'] != p['username'] or prior['generation'] != p['identity_generation'] or
                          prior['watermark'] != event['sequence']):
                        raise ValueError('committed baseline differs from policy origin')
                    continue
                if prior is None or (prior['pending'] or event['sequence'] <= prior['watermark'] or prior['username'] != p['username'] or
                        prior['generation'] != p['identity_generation'] or
                        prior['enabled'] is not p['expected_enabled'] or prior['version'] != p['expected_version']):
                    raise ValueError('priority operation history is not a continuous identity and version chain')
                is_pending = request_id in pending_set
                if event['state'] == 'committed':
                    result = event['result']
                    self.verify_controller_result(event,result,prior['epoch'])
                elif event['state'] != 'pending' or event['result'] is not None:
                    raise ValueError('priority operation state is invalid')
                confirmed_audits = self.outbox._audit_count(event,'priority_confirmed','committed')
                if is_pending:
                    if confirmed_audits:
                        raise ValueError('pending priority operation has a confirmation audit')
                    latest[p['uid']] = {'username':p['username'],'generation':p['identity_generation'],
                                         'enabled':p['expected_enabled'],'version':p['expected_version'],
                                         'pending':True,'request_id':request_id,'epoch':prior['epoch'],'watermark':event['sequence']}
                else:
                    if event['state'] != 'committed' or confirmed_audits != 1:
                        raise ValueError('priority intent is not linked to pending or confirmed state')
                    latest[p['uid']] = {'username':p['username'],'generation':p['identity_generation'],
                                         'enabled':p['enabled'],'version':p['expected_version']+1,
                                         'pending':False,'request_id':None,'epoch':prior['epoch'],'watermark':event['sequence']}
            for uid,last in latest.items():
                current = self.get(uid)
                if (current is None or current['username'] != last['username'] or
                        current['identity_generation'] != last['generation'] or
                        current['enabled'] is not last['enabled'] or current['version'] != last['version'] or
                        current['pending_id'] != last['request_id']):
                    raise ValueError('priority user state does not match its durable operation history')
            if own_transaction:
                self.db.execute('COMMIT')
            return pending
        except BaseException:
            if own_transaction and self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    @staticmethod
    def verify_controller_result(event, result, expected_epoch):
        """Verify applied state before the outbox can advance its sequence."""
        return verify_controller_result(event,result,expected_epoch)

    def apply_confirmation(self, request_id, expected_epoch):
        """Apply an exact durable result from an already-authenticated controller."""
        if not isinstance(expected_epoch,str) or not re.fullmatch(r'[0-9a-f]{32}',expected_epoch):
            raise ValueError('controller epoch is not verified')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            event = self.outbox.status(request_id)
            if event is None or event['state'] != 'committed':
                raise ValueError('controller confirmation is not durable')
            p = event['payload']
            if p.get('action') != 'set_top_priority':
                raise ValueError('unexpected priority action')
            # Even an idempotent retry must have an intact baseline, operation
            # chain and audit; current flags alone cannot prove confirmation.
            self.outbox.verify_sequence_integrity()
            self.pending_confirmations(uid=p['uid'])
            self.verify_controller_result(event,event['result'],expected_epoch)
            current = self.get(p['uid'])
            if not current or current['username'] != p['username'] or current['identity_generation'] != p['identity_generation']:
                raise ValueError('target identity changed')
            if current['pending_id'] is None and current['version'] == p['expected_version']+1 and current['enabled'] is p['enabled']:
                self.db.execute('COMMIT')
                return current
            if current['pending_id'] != request_id or current['version'] != p['expected_version'] or current['enabled'] is not p['expected_enabled']:
                raise ValueError('priority confirmation conflicts with current state')
            changed = self.db.execute('UPDATE top_priority_users SET enabled=?,version=version+1,pending_id=NULL WHERE uid=? AND pending_id=? AND version=? AND enabled=?',
                                      (int(p['enabled']),p['uid'],request_id,p['expected_version'],int(p['expected_enabled']))).rowcount
            if changed != 1:
                raise ValueError('priority state changed concurrently')
            self._audit(event,'priority_confirmed','committed')
            result = self.get(p['uid'])
            self.db.execute('COMMIT')
            return result
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise
