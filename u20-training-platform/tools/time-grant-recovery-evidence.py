#!/usr/bin/env python3
"""Compare one ledger GRANT with supplied approval evidence, without repair.

The caller must independently acquire a consistent offline database snapshot,
validate the complete WAL, witness and scheduler state, and protect originals.
This helper is not a command to repair or authorize restarting a controller.
"""
import json
import re

EVENT = re.compile(rb'U20MQ1\|([1-9][0-9]*)\|GRANT\|([0-9]+)\|([0-9]{6})\|([0-9]+)\|0\|([0-9a-f]{32})\|uid-([0-9]+)\n\Z')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def match_grant(event, outbox, application):
    result = {'intent_matches': False, 'repair_authorized': False,
              'reason': 'grant and approval evidence do not match'}
    if not isinstance(event, bytes) or len(event) > 1023:
        return result
    match = EVENT.fullmatch(event)
    if not match or not isinstance(outbox, dict) or not isinstance(application, dict):
        return result
    try:
        seq, uid, month, seconds, grant_id, actor = match.groups()
        seq, uid, month, seconds, actor = map(int, (seq, uid, month, seconds, actor))
        grant_id = grant_id.decode('ascii')
        if not (seq <= 2**64-1 and uid <= 2**32-1 and actor <= 2**32-1 and
                1970 <= month//100 <= 9999 and 1 <= month%100 <= 12 and
                0 < seconds <= 2**64-1):
            return result
        approved = json.loads(application['approved_json'], object_pairs_hook=unique_object)
        hours = approved['gpu_hours']
        if (type(hours) is not int or not 1 <= hours <= 100000 or
                type(approved['month']) is not int):
            return result
        for row, fields in ((outbox, ('target_uid','month','gpu_seconds','actor_uid')),
                            (application, ('owner_uid',))):
            if any(type(row[k]) is not int for k in fields): return result
        if (outbox['application_id'] != grant_id or application['id'] != grant_id or
                outbox['target_uid'] != uid or application['owner_uid'] != uid or
                outbox['month'] != month or approved['month'] != month or
                outbox['gpu_seconds'] != seconds or hours*3600 != seconds or
                outbox['actor_uid'] != actor or application['kind'] != 'monthly_gpu_hours' or
                (outbox['state'], application['status']) not in
                (('PENDING','GRANT_PENDING'), ('APPLIED','APPROVED'))):
            return result
    except (KeyError, TypeError, ValueError, UnicodeError):
        return result
    return {'intent_matches': True, 'repair_authorized': False,
            'reason': 'intent matches only; independent WAL, scheduler and storage review required'}
