"""Disabled baseline chain encoding; this does not authorize or enqueue policy."""
import hashlib
import re


def baseline_chain_step(previous, epoch, sequence, request, uid, username, generation):
    if (not isinstance(previous, str) or not re.fullmatch(r'[0-9a-f]{64}', previous)
            or not isinstance(epoch, str) or not re.fullmatch(r'[0-9a-f]{32}', epoch)
            or type(sequence) is not int or not 1 <= sequence < 2**63
            or (sequence == 1 and previous != '0' * 64)
            or not isinstance(request, str) or not re.fullmatch(r'[A-Za-z0-9-]{8,80}', request)
            or type(uid) is not int or not 0 <= uid <= 0xffffffff
            or not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', username)
            or not isinstance(generation, str) or not re.fullmatch(r'[0-9a-f]{32}', generation)):
        raise ValueError('invalid baseline chain identity')
    text = '|'.join(('TIME-ORDERED-BASELINE-1', previous, epoch, str(sequence), request,
                     str(uid), username, generation))
    return hashlib.sha256(text.encode('ascii')).hexdigest()
