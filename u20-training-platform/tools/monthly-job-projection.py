#!/usr/bin/env python3
"""Project a *complete offline fixture* of U20MQ1 WAL to per-allocation usage.

This is not an online service, broker API, or a production WAL reader. Callers
must independently verify the fixture's origin, completeness, WAL/checkpoint
relationship and compatibility with their controller version. Open allocations
are reported as incomplete; no running interval is guessed.
"""
import calendar
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

NSEC = 1_000_000_000
SHANGHAI = timezone(timedelta(hours=8))
LIMIT = 2**64 - 1


def _number(value, limit=LIMIT):
    if not value or not value.isascii() or not value.isdigit() or len(value) > 20:
        raise ValueError('invalid integer')
    n = int(value)
    if n > limit:
        raise ValueError('integer overflow')
    return n


def _instant(sec, nano):
    return _number(sec) * NSEC + _number(nano, NSEC - 1)


def _segments(start, end, gpu):
    if start > end:
        raise ValueError('time reversed')
    while start < end:
        local = datetime.fromtimestamp(start // NSEC, SHANGHAI)
        year, month = local.year, local.month
        next_year = year + (month == 12)
        next_month = 1 if month == 12 else month + 1
        boundary = calendar.timegm(datetime(next_year, next_month, 1, tzinfo=SHANGHAI).utctimetuple()) * NSEC
        stop = min(end, boundary)
        amount = (stop - start) * gpu
        if amount > LIMIT * NSEC + (NSEC - 1):
            raise ValueError('usage overflow')
        yield year * 100 + month, amount
        start = stop


def project(data):
    """Return settled nanoseconds by (uid, job, generation, month), or fail.

    No live/unsettled time is included; the second result lists allocations
    still active at the end of the fixture. Unknown events fail closed.
    """
    if not isinstance(data, bytes) or len(data) > 64 * 1024 * 1024 or not data.endswith(b'\n'):
        raise ValueError('incomplete or oversized WAL fixture')
    lines = data.decode('ascii').splitlines()
    if not lines or lines[0] != 'U20MQ1|HEADER|1':
        raise ValueError('invalid WAL header')
    active, usage, totals, grants, grant_totals = {}, {}, {}, {}, {}
    seq, next_generation = 0, 1
    for line in lines[1:]:
        fields = line.split('|')
        if len(line) > 1023 or len(fields) < 3 or fields[0] != 'U20MQ1':
            raise ValueError('invalid WAL event')
        current = _number(fields[1])
        if current != seq + 1:
            raise ValueError('non-contiguous WAL sequence')
        seq = current
        event = fields[2]
        if event == 'GRANT':
            if len(fields) != 9:
                raise ValueError('invalid grant')
            uid, month = _number(fields[3], 2**32-1), _number(fields[4], 2**32-1)
            amount = _instant(fields[5], fields[6])
            if (not amount or amount % NSEC or month // 100 < 1970 or
                    month // 100 > 9999 or not 1 <= month % 100 <= 12):
                raise ValueError('invalid grant amount or month')
            if any(not x or len(x) >= 128 or any(c in x for c in '|\r\n \t\x00') for x in fields[7:]):
                raise ValueError('invalid grant field')
            grant_id = fields[7]
            if grant_id in grants:
                raise ValueError('duplicate grant identifier')
            total_key = (uid, month)
            grant_totals[total_key] = grant_totals.get(total_key, 0) + amount
            if grant_totals[total_key] > LIMIT * NSEC + (NSEC - 1):
                raise ValueError('grant total overflow')
            grants[grant_id] = (uid, month, amount, fields[8])
            continue
        if event == 'PREPARE' and len(fields) == 9:
            job, uid = _number(fields[3], 2**32-1), _number(fields[4], 2**32-1)
            generation, gpu = _number(fields[5]), _number(fields[6])
            key = (job, generation)
            if not gpu or generation >= LIMIT or generation < next_generation or any(k[0] == job for k in active):
                raise ValueError('invalid or reused allocation identity')
            next_generation = generation + 1
            active[key] = {'uid': uid, 'gpu': gpu, 'anchor': _instant(fields[7], fields[8]), 'committed': False}
            continue
        if len(fields) < 5:
            raise ValueError('invalid allocation event')
        key = (_number(fields[3], 2**32-1), _number(fields[4]))
        if key not in active:
            raise ValueError('unknown allocation')
        a = active[key]
        if event == 'COMMIT' and len(fields) == 5 and not a['committed']:
            a['committed'] = True
        elif event == 'ABORT' and len(fields) == 5 and not a['committed']:
            del active[key]
        elif event in ('SETTLE', 'RELEASE'):
            if event == 'SETTLE':
                if len(fields) != 9 or not a['committed'] or _instant(fields[5], fields[6]) != a['anchor']:
                    raise ValueError('invalid settlement')
                end = _instant(fields[7], fields[8])
            else:
                if len(fields) != 7 or not a['committed']:
                    raise ValueError('invalid release')
                end = _instant(fields[5], fields[6])
            for month, amount in _segments(a['anchor'], end, a['gpu']):
                identity = (a['uid'], key[0], key[1], month)
                usage[identity] = usage.get(identity, 0) + amount
                if usage[identity] > LIMIT * NSEC + (NSEC - 1):
                    raise ValueError('usage overflow')
                total_key = (a['uid'], month)
                totals[total_key] = totals.get(total_key, 0) + amount
                if totals[total_key] > LIMIT * NSEC + (NSEC - 1):
                    raise ValueError('monthly total overflow')
            a['anchor'] = end
            if event == 'RELEASE':
                del active[key]
        else:
            raise ValueError('unexpected event or state')
    return usage, active, seq


def project_verified_fixture(data, checkpoint):
    """Check a caller-supplied offline WAL/checkpoint pair before projecting.

    The matching sequence does not authenticate the pair or prove it is fresh;
    callers must provide a consistent, trusted, controller-version-matched pair.
    """
    prefix = b'U20MQ1|CHECKPOINT|'
    if (not isinstance(checkpoint, bytes) or len(checkpoint) >= 160 or
            not checkpoint.startswith(prefix) or not checkpoint.endswith(b'\n')):
        raise ValueError('invalid checkpoint fixture')
    try:
        fields = checkpoint[len(prefix):-1].decode('ascii').split('|')
        if len(fields) != 2 or len(fields[1]) != 64 or any(c not in '0123456789abcdef' for c in fields[1]):
            raise ValueError('checkpoint must include a lowercase SHA-256')
        recorded = _number(fields[0])
    except UnicodeDecodeError as exc:
        raise ValueError('invalid checkpoint encoding') from exc
    if not hmac.compare_digest(fields[1], hashlib.sha256(data).hexdigest()):
        raise ValueError('checkpoint does not match WAL digest')
    usage, active, seq = project(data)
    if recorded != seq:
        raise ValueError('checkpoint does not match WAL sequence')
    return usage, active, seq
