"""Isolated top-marker intent journal; not connected to production submission."""
import re
import sqlite3


class IntentConflict(ValueError):
    pass


class TopPendingIntents:
    def __init__(self, path=None, *, db=None):
        if (path is None) == (db is None):
            raise ValueError('supply exactly one database path or connection')
        self.db = db if db is not None else sqlite3.connect(path, isolation_level=None, timeout=10)
        if self.db.isolation_level is not None or self.db.in_transaction:
            raise ValueError('an idle autocommit connection is required')
        if db is None:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS top_pending_intents (
            marker TEXT PRIMARY KEY, uid INTEGER NOT NULL, account_generation TEXT NOT NULL,
            request_id TEXT NOT NULL, request_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('pending','registered','cancel_pending','cancelled')),
            UNIQUE(uid, request_id))""")
        schema = self.db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='top_pending_intents'").fetchone()
        if schema is None or "'cancelled'" not in schema[0]:
            raise ValueError('priority marker schema requires reviewed migration')

    def prepare(self, marker, uid, account_generation, request_id, request_hash, *, confirmed_top):
        """Join caller's transaction, or own one; caller rolls back on failure.

        Controller registration and cancellation ordering are not implemented.
        `confirmed_top` must come from a trusted, reconciled policy source.
        """
        if (type(confirmed_top) is not bool
                or not isinstance(marker, str) or not re.fullmatch(r"[0-9a-f]{32}", marker)
                or type(uid) is not int or not 0 <= uid <= 0xffffffff
                or not isinstance(account_generation, str) or not re.fullmatch(r"[0-9a-f]{32}", account_generation)
                or not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,80}", request_id)
                or not isinstance(request_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", request_hash)):
            raise ValueError("invalid intent identity")
        identity = (marker, uid, account_generation, request_id, request_hash)
        own_transaction = not self.db.in_transaction
        if own_transaction:
            self.db.execute("BEGIN IMMEDIATE")
        try:
            rows = self.db.execute("SELECT marker,uid,account_generation,request_id,request_hash,state "
                                   "FROM top_pending_intents WHERE marker=? OR (uid=? AND request_id=?)",
                                   (marker, uid, request_id)).fetchall()
            if rows:
                if len(rows) != 1 or tuple(rows[0][:5]) != identity:
                    raise IntentConflict("conflicting registration identity")
                state = rows[0][5]
            elif confirmed_top is True:
                self.db.execute("INSERT INTO top_pending_intents VALUES(?,?,?,?,?,'pending')", identity)
                state = 'pending'
            else:
                state = None
            if own_transaction:
                self.db.execute("COMMIT")
            return state
        except BaseException:
            if own_transaction and self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def mark_registered(self, marker):
        return self.db.execute("UPDATE top_pending_intents SET state='registered' "
                               "WHERE marker=? AND state='pending'", (marker,)).rowcount == 1

    def request_cancel(self, marker):
        return self.db.execute("UPDATE top_pending_intents SET state='cancel_pending' "
                               "WHERE marker=? AND state IN ('pending','registered')", (marker,)).rowcount == 1
