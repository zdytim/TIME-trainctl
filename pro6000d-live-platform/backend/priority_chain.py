"""Reconstruct the experimental controller chain; this is not an anchor.

The hash detects differing histories only when compared to an authenticated
controller value and an independently protected durable broker anchor.
Neither facility is wired or enabled by this module.
"""
import hashlib
import json
import re

from priority_protocol import validate_epoch, validate_priority_payload
from priority_baseline_chain import baseline_chain_step


def chain_step(previous, epoch, event, previous_sequence):
    validate_epoch(epoch)
    if not isinstance(previous, str) or not re.fullmatch(r'[0-9a-f]{64}', previous):
        raise ValueError('invalid prior chain hash')
    if type(previous_sequence) is not int or not 0 <= previous_sequence < 2**63 - 1:
        raise ValueError('invalid prior sequence')
    if not isinstance(event, dict) or type(event.get('sequence')) is not int or event['sequence'] != previous_sequence + 1:
        raise ValueError('priority chain sequence is not contiguous')
    if previous_sequence == 0 and previous != '0' * 64:
        raise ValueError('priority chain genesis is invalid')
    request = event.get('request_id')
    if not isinstance(request, str) or not re.fullmatch(r'[A-Za-z0-9-]{8,80}', request):
        raise ValueError('invalid priority request id')
    payload = event.get('payload')
    if not isinstance(payload, dict):
        raise ValueError('invalid priority payload')
    digest = event.get('digest')
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or hashlib.sha256(encoded).hexdigest() != digest:
        raise ValueError('priority payload digest mismatch')
    if payload.get('action') == 'baseline_top_priority':
        if set(payload) != {'action', 'uid', 'username', 'identity_generation'}:
            raise ValueError('invalid baseline payload')
        return baseline_chain_step(previous, epoch, event['sequence'], request,
                                   payload['uid'], payload['username'], payload['identity_generation'])
    validate_priority_payload(payload)
    fields = ('TIME-ORDERED-TOP-CHAIN-1', previous, epoch, str(event['sequence']), request,
              digest, str(payload['uid']), str(int(payload['enabled'])),
              str(payload['expected_version'] + 1), payload['identity_generation'])
    return hashlib.sha256('|'.join(fields).encode('ascii')).hexdigest()


class AuthenticatedPrefixVerifier:
    """Compare a local prefix with a boot-bound remote hash and trusted anchor.

    Both callbacks must authenticate their sources. No production controller
    chain transport or independently protected anchor is provided here.
    """
    def __init__(self, chain_lookup, anchor_reader):
        if not callable(chain_lookup) or not callable(anchor_reader):
            raise ValueError('authenticated chain and anchor sources required')
        self.chain_lookup, self.anchor_reader = chain_lookup, anchor_reader

    def __call__(self, status, committed, outbox):
        if (not isinstance(status, dict) or type(committed) is not int or committed < 0 or
                not isinstance(status.get('boot'), str) or not re.fullmatch(r'[0-9a-f]{32}', status['boot'])):
            return False
        try:
            epoch = status['epoch']
            validate_epoch(epoch)
            anchor = self.anchor_reader()
            if (not isinstance(anchor, dict) or anchor.get('epoch') != epoch or
                    type(status.get('watermark')) is not int or
                    type(anchor.get('sequence')) is not int or anchor['sequence'] not in (committed, committed + 1) or
                    anchor['sequence'] > status['watermark'] or
                    not isinstance(anchor.get('hash'), str) or not re.fullmatch(r'[0-9a-f]{64}', anchor['hash'])):
                return False
            # The anchor may be one event ahead after its fsync but before the
            # local outbox confirmation; that event must be the exact pending one.
            checked_sequence = max(committed, anchor['sequence'])
            db = outbox.db
            own_transaction = not db.in_transaction
            if own_transaction:
                db.execute('BEGIN')
            try:
                outbox.verify_sequence_integrity()
                digest = '0' * 64
                anchored = digest if anchor['sequence'] == 0 else None
                rows = db.execute('SELECT request_id FROM top_priority_outbox WHERE sequence<=? ORDER BY sequence', (checked_sequence,)).fetchall()
                if len(rows) != checked_sequence:
                    raise ValueError('local committed history is incomplete')
                for index, (request_id,) in enumerate(rows, 1):
                    event = outbox.status(request_id)
                    expected_state = 'committed' if index <= committed else 'pending'
                    if event is None or event['state'] != expected_state:
                        raise ValueError('local prefix contains an uncommitted operation')
                    digest = chain_step(digest, epoch, event, index - 1)
                    if index == anchor['sequence']:
                        anchored = digest
                if own_transaction:
                    db.execute('COMMIT')
            except BaseException:
                if own_transaction and db.in_transaction:
                    db.execute('ROLLBACK')
                raise
            if anchored != anchor['hash']:
                return False
            # Lookup must authenticate the exact installation and process boot
            # from status, and read the requested sequence in one controller.
            return self.chain_lookup(status, checked_sequence) == digest
        except (ValueError, KeyError, TypeError):
            return False


class ControllerAnchorLinkVerifier:
    """Verify old and new hashes against one pinned controller boot.

    The transport must independently authenticate its peer for every query;
    the caller must serialize all broker deliveries and anchor advancement.
    """
    def __init__(self, transport):
        self.transport = transport

    def __call__(self, last, epoch, sequence, digest):
        if (not isinstance(last, dict) or last.get('epoch') != epoch or
                type(last.get('sequence')) is not int or last['sequence'] not in (sequence - 1, sequence) or
                not isinstance(last.get('hash'), str) or not re.fullmatch(r'[0-9a-f]{64}', last['hash'])):
            return False
        status = self.transport.status()
        if status.get('epoch') != epoch or status.get('watermark') != sequence:
            return False
        previous = self.transport.chain_lookup(status, last['sequence'])
        current = self.transport.chain_lookup(status, sequence)
        return (previous == last['hash'] and current == digest and
                self.transport.status() == status)
