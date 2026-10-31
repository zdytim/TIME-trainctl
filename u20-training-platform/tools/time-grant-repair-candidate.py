#!/usr/bin/env python3
"""Build in-memory GRANT marker candidates; never write files or authorize repair.

Supplying matching approval rows is not sufficient evidence of a valid WAL or
safe scheduler state. A caller must preserve originals and independently
validate full replay, persistence and running-job reconciliation.
"""
import hashlib
import importlib.util
from pathlib import Path


def load(name, filename):
    path = Path(__file__).resolve().parent/filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


inspect = load('time_recovery_inspect', 'time-quota-recovery-inspect.py')
evidence = load('time_grant_evidence', 'time-grant-recovery-evidence.py')


def candidate(wal, checkpoint, witness, outbox, application):
    if any(not isinstance(value, bytes) or len(value) > inspect.LIMIT
           for value in (wal, checkpoint, witness)):
        raise ValueError('invalid or oversized input')
    observed = inspect.inspect(wal, checkpoint, witness)
    if observed['observation'] not in ('wal_one_event_ahead','witness_one_event_behind'):
        raise ValueError('ledger does not have an eligible marker shape')
    lines = wal.splitlines(keepends=True)
    match = evidence.match_grant(lines[-1], outbox, application)
    if not match['intent_matches']:
        raise ValueError('final grant does not match approval evidence')
    grant_id = outbox['application_id'].encode('ascii')
    for line in lines[1:-1]:
        fields = line.split(b'|')
        if len(fields) > 7 and fields[2] == b'GRANT' and fields[7] == grant_id:
            raise ValueError('grant id already appears in earlier WAL')
    seq = observed['wal_sequence']
    digest = hashlib.sha256(wal).hexdigest().encode('ascii')
    proposed_checkpoint = b'U20MQ1|CHECKPOINT|'+str(seq).encode()+b'|'+digest+b'\n'
    proposed_witness = witness+b'U20MQA1|'+str(seq).encode()+b'|'+digest+b'\n'
    if not inspect.inspect(wal, proposed_checkpoint, proposed_witness)['matching']:
        raise ValueError('candidate markers do not match WAL')
    return {'checkpoint':proposed_checkpoint, 'witness':proposed_witness,
            'repair_authorized':False,
            'note':'candidate bytes only; full semantic replay, scheduler and durability review required'}
