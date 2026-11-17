"""Strict record decoding only; this is not a durable or authenticated anchor."""

import re

_RECORD = re.compile(rb"([0-9a-f]{32}) (0|[1-9][0-9]{0,19}) ([0-9a-f]{64})\n\Z")
_ZERO = "0" * 64


def parse_records(stream):
    """Validate a complete bounded-record stream and return its last tuple.

    A caller must separately validate file ownership, lock it, and prove the
    authenticated origin of hashes. This function does none of those things.
    """
    epoch = None
    expected = 0
    last = None
    while True:
        line = stream.readline(120)
        if not line:
            break
        match = _RECORD.fullmatch(line)
        if match is None:
            raise ValueError("malformed or incomplete anchor record")
        current_epoch = match[1].decode("ascii")
        sequence = int(match[2])
        digest = match[3].decode("ascii")
        if (sequence != expected or sequence > 2**64 - 1 or
                (epoch is not None and epoch != current_epoch) or
                (sequence == 0 and digest != _ZERO)):
            raise ValueError("invalid anchor history")
        epoch = current_epoch
        last = (epoch, sequence, digest)
        expected += 1
    if last is None:
        raise ValueError("missing genesis")
    return last
