"""Account generations tied to externally verified lifecycle identities.

A UID/name pair alone cannot distinguish account deletion and recreation. The
caller must supply an authoritative immutable account identifier and must not
create or update bindings merely because a passwd lookup succeeded.
"""
import hashlib
import hmac
import re


class AccountLifecycleRegistry:
    def __init__(self, db, identity_provider):
        if not callable(identity_provider):
            raise ValueError('authoritative account identity provider required')
        self.db, self.identity_provider = db, identity_provider
        db.execute('CREATE TABLE IF NOT EXISTS verified_account_lifecycle '
                   '(uid INTEGER PRIMARY KEY, username TEXT NOT NULL, identity_hash TEXT NOT NULL, '
                   'generation TEXT NOT NULL)')

    def __call__(self, user):
        token = self.identity_provider(user)
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise ValueError('verified account lifecycle identity unavailable')
        digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
        row = self.db.execute('SELECT username,identity_hash,generation FROM verified_account_lifecycle WHERE uid=?',
                              (user.pw_uid,)).fetchone()
        if (row is None or row[0] != user.pw_name or not hmac.compare_digest(row[1], digest) or
                not isinstance(row[2], str) or not re.fullmatch(r'[0-9a-f]{32}', row[2])):
            raise ValueError('account lifecycle identity not provisioned or changed')
        return row[2]
