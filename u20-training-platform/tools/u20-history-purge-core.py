#!/usr/bin/env python3
"""Transactional database primitive for an offline, protected cutover only.

This module has no CLI and does not stop services, inspect Slurm, back up data,
or clear jobcomp. Its caller must establish and hold those prerequisites.
"""
import sqlite3

TABLES = frozenset(('settings', 'quota_overrides', 'jobs', 'applications',
                    'notifications', 'audit_log', 'rule_previews', 'quota_previews',
                    'job_request_fingerprints', 'monthly_request_keys',
                    'monthly_grant_outbox', 'job_request_tombstones'))
TERMINAL = frozenset(('COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY',
                      'NODE_FAIL', 'BOOT_FAIL', 'DEADLINE', 'PREEMPTED', 'EXPIRED'))


def purge(conn, precheck=None):
    if conn.in_transaction or conn.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise ValueError('requires foreign keys and an idle connection')
    conn.execute('BEGIN IMMEDIATE')
    try:
        # The caller may compare the locked live database with a verified
        # cutover backup before any trigger or deletion is written.
        if precheck is not None:
            precheck(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables != TABLES:
            raise ValueError('unexpected database schema')
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('jobs','job_request_fingerprints','job_request_tombstones') LIMIT 1").fetchone():
            raise ValueError('unexpected job-history trigger')
        fingerprint_reference = False
        for table in tables:
            # Table names are restricted to the constant allowlist above.
            for fk in conn.execute('PRAGMA foreign_key_list("' + table + '")'):
                if fk[2] == 'jobs' and not (table == 'job_request_fingerprints' and fk[3:7] == ('job_id', 'id', 'NO ACTION', 'CASCADE')):
                    raise ValueError('unexpected job reference')
                if fk[2] == 'jobs':
                    fingerprint_reference = True
        if not fingerprint_reference:
            raise ValueError('expected fingerprint cascade is missing')
        rows = conn.execute('SELECT id,state,finished_at,temporary_application_id FROM jobs').fetchall()
        if any(state not in TERMINAL or not finished or app is not None for _, state, finished, app in rows):
            raise ValueError('active, ambiguous or approval-linked job exists')
        if conn.execute('SELECT 1 FROM applications WHERE bound_job_id IS NOT NULL LIMIT 1').fetchone():
            raise ValueError('approval is bound to a job')
        # Database-level guards remain effective even if an older broker that
        # does not know about tombstones is later started against this DB.
        # Create them in the same transaction as the tombstones and deletion.
        condition = ('NEW.client_request_id IS NOT NULL AND EXISTS ('
                     'SELECT 1 FROM job_request_tombstones WHERE owner_uid=NEW.owner_uid '
                     'AND client_request_id=NEW.client_request_id)')
        for event, name in (('INSERT', 'time_history_request_insert_guard'),
                            ('UPDATE OF owner_uid,client_request_id', 'time_history_request_update_guard')):
            conn.execute('CREATE TRIGGER ' + name + ' BEFORE ' + event + ' ON jobs '
                         'WHEN ' + condition + " BEGIN SELECT RAISE(ABORT, 'historical request id retired'); END")
        conn.execute('INSERT INTO job_request_tombstones(owner_uid,client_request_id) '
                     'SELECT owner_uid,client_request_id FROM jobs WHERE client_request_id IS NOT NULL '
                     'ON CONFLICT(owner_uid,client_request_id) DO NOTHING')
        missing = conn.execute('SELECT 1 FROM jobs j LEFT JOIN job_request_tombstones t '
                               'ON t.owner_uid=j.owner_uid AND t.client_request_id=j.client_request_id '
                               'WHERE j.client_request_id IS NOT NULL AND t.client_request_id IS NULL LIMIT 1').fetchone()
        if missing:
            raise ValueError('historical request identity was not preserved')
        conn.execute('DELETE FROM jobs')
        remaining = conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
        if remaining or conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('post-delete integrity check failed')
        conn.execute('COMMIT')
        return len(rows)
    except BaseException:
        conn.execute('ROLLBACK')
        raise
