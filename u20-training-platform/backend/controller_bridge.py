"""Staged root-broker client for the Slurm monthly quota control socket.

No direct browser access. The broker must separately authenticate the Unix
peer, authorize grants and persist approval/audit state before invoking grant.
"""
import os
import re
import socket
import stat
import struct


class ControllerUnavailable(Exception):
    pass


class ControllerOutcomeUnknown(ControllerUnavailable):
    """The controller may have applied a mutation; reconcile by grant ID."""


class ControllerBridge:
    def __init__(self, path, controller_uid, timeout=2.0):
        self.path = path
        self.controller_uid = int(controller_uid)
        self.timeout = timeout

    def _connect(self):
        if os.geteuid() != 0:
            raise ControllerUnavailable("controller bridge requires the root broker")
        try:
            directory = os.stat(os.path.dirname(self.path), follow_symlinks=False)
            endpoint = os.stat(self.path, follow_symlinks=False)
            if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != self.controller_uid or
                    directory.st_mode & 0o022 or not stat.S_ISSOCK(endpoint.st_mode) or
                    endpoint.st_uid != self.controller_uid or endpoint.st_mode & 0o077):
                raise ControllerUnavailable("untrusted controller socket or directory")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            try:
                sock.connect(self.path)
                pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != self.controller_uid or pid <= 0:
                    raise ControllerUnavailable("controller peer identity mismatch")
                return sock
            except Exception:
                sock.close()
                raise
        except ControllerUnavailable:
            raise
        except (OSError, ValueError, struct.error) as exc:
            raise ControllerUnavailable("controller connection unavailable") from exc

    def _exchange(self, message, mutation=False):
        sent = False
        try:
            with self._connect() as sock:
                # From the start of sendall, partial delivery is possible.
                sent = True
                sock.sendall(message.encode("ascii") + b"\n")
                data = bytearray()
                while len(data) <= 1024 and b"\n" not in data:
                    chunk = sock.recv(min(1025 - len(data), 512))
                    if not chunk:
                        break
                    data.extend(chunk)
                if len(data) > 1024 or not data.endswith(b"\n") or data.count(b"\n") != 1:
                    raise ControllerUnavailable("controller returned an incomplete response")
                line = bytes(data[:-1]).decode("ascii")
                if line == "ERROR unavailable_or_invalid":
                    raise ControllerUnavailable("controller rejected or is not ready")
                return line
        except (OSError, UnicodeError, ControllerUnavailable) as exc:
            if mutation and sent:
                raise ControllerOutcomeUnknown("grant result unknown; reconcile using the same grant ID and fields") from exc
            if isinstance(exc, ControllerUnavailable):
                raise
            raise ControllerUnavailable("controller request failed") from exc

    @staticmethod
    def _uint(value, upper):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= upper:
            raise ValueError("invalid unsigned integer")
        return value

    def snapshot(self, uid):
        uid = self._uint(uid, 2**32 - 1)
        line = self._exchange("SNAP %d" % uid)
        fields = line.split(" ")
        if len(fields) != 18 or fields[0] != "OK" or any(not re.fullmatch(r"[0-9]+", x) for x in fields[1:]):
            raise ControllerUnavailable("invalid controller snapshot")
        values = [int(x) for x in fields[1:]]
        month, tier = values[:2]
        if not 197001 <= month <= 999912 or not 1 <= month % 100 <= 12 or tier > 3:
            raise ControllerUnavailable("invalid controller snapshot metadata")
        if any(values[index] >= 10**9 for index in (3, 5, 7, 9, 11, 15)):
            raise ControllerUnavailable("invalid fractional GPU seconds or timestamp")
        def amount(index):
            return {"gpu_seconds": values[index], "nanoseconds": values[index + 1]}
        return {"uid": uid, "month": month, "tier": ("debug", "important", "normal", "exhausted")[tier],
                "used": amount(2), "granted": amount(4), "capacity": amount(6),
                "remaining": amount(8), "overage": amount(10),
                "active_gpu_count": values[12], "active_allocation_count": values[13],
                "observed_sec": values[14], "observed_nsec": values[15], "wal_sequence": values[16]}

    def grant(self, uid, month, gpu_seconds, grant_id, actor):
        uid = self._uint(uid, 2**32 - 1)
        month = self._uint(month, 999912)
        gpu_seconds = self._uint(gpu_seconds, 2**64 - 1)
        if (month < 197001 or not 1 <= month % 100 <= 12 or not gpu_seconds or
                any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,127}", x)
                    for x in (grant_id, actor))):
            raise ValueError("invalid grant fields")
        line = self._exchange("GRANT %d %d %d %s %s" % (uid, month, gpu_seconds, grant_id, actor), mutation=True)
        if line != "OK":
            raise ControllerOutcomeUnknown("unexpected grant reply; reconcile using the same grant ID and fields")
        return {"applied": True, "grant_id": grant_id}
