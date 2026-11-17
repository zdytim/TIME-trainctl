"""Experimental protected anchor file access; not wired to the service."""
from contextlib import contextmanager
import fcntl
import io
import os
import re
import stat

from ordered_anchor_records import parse_records

MAX_BYTES = 16 * 1024 * 1024


@contextmanager
def _locked_anchor(directory, writable=False):
    """Open the anchor after locking the shared, never-replaced lock inode."""
    if not isinstance(directory, str) or not directory.startswith('/'):
        raise ValueError('absolute directory required')
    components = directory[1:].split('/')
    if any(not part or part in ('.', '..') or len(os.fsencode(part)) > 255 for part in components):
        raise ValueError('invalid directory component')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_fd = os.open('/', flags)
    try:
        for component in [None, *components]:
            if component is not None:
                next_fd = os.open(component, flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            info = os.fstat(directory_fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError('untrusted anchor directory')
        lock_fd = os.open('ordered-chain.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory_fd)
        try:
            info = os.fstat(lock_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
                    stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise ValueError('untrusted anchor lock')
            # A busy writer (or an accidentally reused delivery lock) must
            # fail closed rather than hang the broker indefinitely.
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            access = (os.O_RDWR | os.O_APPEND) if writable else os.O_RDONLY
            fd = os.open('ordered-chain.anchor', access | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory_fd)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
                        stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                    raise ValueError('untrusted anchor file')
                yield fd
            finally:
                os.close(fd)
        finally:
            os.close(lock_fd)
    finally:
        os.close(directory_fd)


def _read_locked(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    data = bytearray()
    while len(data) <= MAX_BYTES:
        chunk = os.read(fd, min(65536, MAX_BYTES + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    if len(data) > MAX_BYTES:
        raise ValueError('anchor exceeds size limit')
    epoch, sequence, digest = parse_records(io.BytesIO(data))
    return {'epoch': epoch, 'sequence': sequence, 'hash': digest}, len(data)


def read_anchor(directory):
    """Sync and read an existing private anchor under the shared lock.

    A prior writer may have lost its fsync acknowledgement; reading cached
    bytes alone must not treat them as durable.
    """
    with _locked_anchor(directory, writable=True) as fd:
        try:
            os.fsync(fd)
        except OSError as exc:
            raise AnchorOutcomeUnknown('anchor durability unknown') from exc
        record, _ = _read_locked(fd)
        return record


class AnchorOutcomeUnknown(RuntimeError):
    """An append may be partial or durable; re-read before any recovery."""


def append_anchor(directory, epoch, sequence, digest, link_verifier):
    """Append one authenticated, contiguous record; never repair a torn file.

    The verifier must authenticate, on the same controller boot, BOTH the
    existing anchor prefix and the exact new controller chain hash.
    Provisioning, anchor advancement coordination and external rollback
    protection are not supplied by this experimental component.
    """
    if (not isinstance(epoch, str) or not re.fullmatch(r'[0-9a-f]{32}', epoch) or
            type(sequence) is not int or not 1 <= sequence <= 2**63 - 1 or
            not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or
            not callable(link_verifier)):
        raise ValueError('invalid anchor update')
    with _locked_anchor(directory, writable=True) as fd:
        last, size = _read_locked(fd)
        if last['epoch'] != epoch:
            raise ValueError('anchor installation identity changed')
        if link_verifier(last, epoch, sequence, digest) is not True:
            raise ValueError('controller history link is not authenticated')
        if sequence == last['sequence']:
            if digest != last['hash']:
                raise ValueError('anchor retry conflicts with history')
            # A previous append may have written the whole line but lost its
            # fsync acknowledgement. Sync again before reporting durability.
            try:
                os.fsync(fd)
            except OSError as exc:
                raise AnchorOutcomeUnknown('anchor retry durability unknown') from exc
            return last
        if sequence != last['sequence'] + 1:
            raise ValueError('anchor update is not contiguous')
        line = ('{} {} {}\n'.format(epoch, sequence, digest)).encode('ascii')
        if size + len(line) > MAX_BYTES:
            raise ValueError('anchor exceeds size limit')
        try:
            offset = 0
            while offset < len(line):
                count = os.write(fd, line[offset:])
                if count <= 0:
                    raise OSError('short anchor write')
                offset += count
            os.fsync(fd)
        except OSError as exc:
            raise AnchorOutcomeUnknown('anchor append outcome unknown') from exc
        return {'epoch': epoch, 'sequence': sequence, 'hash': digest}
