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
                raise ControllerOutcomeUnknown("controller mutation outcome unknown; reconcile using the same identity and fields") from exc
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

    def selectable_snapshot(self, uid):
        """Read a versioned per-tier snapshot; the legacy controller rejects SNAP2.

        Parsing does not prove enforcement has been activated. Callers must
        independently verify the controller version and cutover before exposing
        selectable tier capability to clients.
        """
        uid = self._uint(uid, 2**32 - 1)
        fields = self._exchange("SNAP2 %d" % uid).split(" ")
        if (len(fields) != 18 or fields[0] != "OK" or fields[1] != "2" or
                any(not re.fullmatch(r"[0-9]+", x) for x in fields[2:])):
            raise ControllerUnavailable("invalid selectable controller snapshot")
        values = [int(x) for x in fields[2:]]
        if any(x > 2**64 - 1 for x in values):
            raise ControllerUnavailable("selectable controller snapshot overflow")
        month = values[0]
        if not 197001 <= month <= 999912 or not 1 <= month % 100 <= 12:
            raise ControllerUnavailable("invalid selectable snapshot month")
        if any(values[i] >= 10**9 for i in (2, 4, 6, 8, 10, 12)):
            raise ControllerUnavailable("invalid selectable snapshot fraction")
        if values[14] > 2**32 - 1:
            raise ControllerUnavailable("selectable allocation count overflow")
        def amount(index):
            return {"gpu_seconds": values[index], "nanoseconds": values[index + 1]}
        return {"schema_version": 2, "uid": uid, "month": month,
                "granted": amount(1),
                "tiers": {"debug": {"used": amount(3)},
                          "important": {"used": amount(5)},
                          "normal": {"used": amount(7)}},
                "overage": amount(9), "observed_sec": values[11],
                "observed_nsec": values[12], "wal_sequence": values[13],
                "active_allocation_count": values[14], "active_gpu_count": values[15]}

    def selectable_cutover_reset_usage(self):
        """For a fenced, reviewed deployment only; not exposed to API users.

        The controller requires an experimental-enabled build and no active or
        queued jobs. An ambiguous result must be reconciled from the trusted
        V2 snapshot and ledger; do not blindly retry or roll back live work.
        """
        try:
            line = self._exchange("CUTOVER2_RESET_USAGE 0", mutation=True)
        except ControllerOutcomeUnknown as exc:
            raise ControllerOutcomeUnknown("cutover outcome unknown; reconcile controller ledger before proceeding") from exc
        if line != "OK":
            raise ControllerOutcomeUnknown("unexpected cutover reply; reconcile controller ledger before proceeding")
        return {"applied": True}

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

    @staticmethod
    def _reservation_request(uid, request_id):
        uid = ControllerBridge._uint(uid, 2**32 - 1)
        if not uid or not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id, re.ASCII):
            raise ValueError("invalid reservation identity")
        return uid, request_id

    def reserve(self, uid, request_id, tier, gpu, minutes):
        """Request a controller-owned hold; unknown replies require reconciliation.

        This transport does not establish that the allocation gate is active.
        """
        uid, request_id = self._reservation_request(uid, request_id)
        tier_ids = {"debug": 0, "important": 1, "normal": 2}
        if not isinstance(tier, str) or tier not in tier_ids:
            raise ValueError("invalid reservation tier")
        gpu = self._uint(gpu, 2**32 - 1)
        minutes = self._uint(minutes, 2**32 - 1)
        amount = gpu * minutes * 60 * 10**9
        if not gpu or not minutes or amount > 2**64 - 1:
            raise ValueError("invalid reservation amount")
        line = self._exchange("RESERVE1 %d %s %d %d %d" %
                              (uid, request_id, tier_ids[tier], gpu, minutes), mutation=True)
        if line != "OK %d" % amount:
            raise ControllerOutcomeUnknown("unexpected reserve reply; reconcile the same request ID")
        return {"request_id": request_id, "reserved_gpu_nanoseconds": amount}

    def reservation(self, uid, request_id):
        """Read the hold fields; this protocol does not expose binding state."""
        uid, request_id = self._reservation_request(uid, request_id)
        fields = self._exchange("RESERVE1_GET %d %s" % (uid, request_id)).split(" ")
        if len(fields) != 6 or fields[0] != "OK" or any(not re.fullmatch(r"[0-9]+", x) for x in fields[1:]):
            raise ControllerUnavailable("invalid reservation response")
        month, tier, gpu, minutes, amount = (int(x) for x in fields[1:])
        if (not 197001 <= month <= 999912 or not 1 <= month % 100 <= 12 or tier > 2 or
                not 1 <= gpu <= 2**32 - 1 or not 1 <= minutes <= 2**32 - 1 or
                amount > 2**64 - 1 or amount != gpu * minutes * 60 * 10**9):
            raise ControllerUnavailable("invalid reservation fields")
        return {"uid": uid, "request_id": request_id, "month": month,
                "tier": ("debug", "important", "normal")[tier], "gpu": gpu,
                "minutes": minutes, "reserved_gpu_nanoseconds": amount}

    def bind_reservation(self, uid, request_id, scheduler_id):
        """Bind only a confirmed pending job; never assume a lost reply failed."""
        uid, request_id = self._reservation_request(uid, request_id)
        scheduler_id = self._uint(scheduler_id, 2**32 - 1)
        if not scheduler_id:
            raise ValueError("invalid scheduler job ID")
        line = self._exchange("BIND1 %d %s %d" % (uid, request_id, scheduler_id), mutation=True)
        if line != "OK":
            raise ControllerOutcomeUnknown("unexpected bind reply; reconcile request and job identity")
        return {"request_id": request_id, "scheduler_id": scheduler_id, "bound": True}

    def reservation_status(self, uid, request_id):
        """Read live binding state; absence is not proof of no prior effect."""
        uid, request_id = self._reservation_request(uid, request_id)
        fields = self._exchange("RESERVE2_GET %d %s" % (uid, request_id)).split(" ")
        if fields == ["OK", "2", "ABSENT"]:
            return {"uid": uid, "request_id": request_id, "present": False}
        if (len(fields) != 12 or fields[:3] != ["OK", "2", "FOUND"] or
                any(not re.fullmatch(r"[0-9]+", x) for x in fields[3:])):
            raise ControllerUnavailable("invalid reservation status response")
        month, tier, gpu, minutes, amount, job, generation, state, sluid = (int(x) for x in fields[3:])
        if (not 197001 <= month <= 999912 or not 1 <= month % 100 <= 12 or tier > 2 or
                not 1 <= gpu <= 2**32 - 1 or not 1 <= minutes <= 2**32 - 1 or
                amount > 2**64 - 1 or amount != gpu * minutes * 60 * 10**9 or
                job > 2**32 - 1 or generation > 2**64 - 1 or sluid > 2**64 - 1 or state > 3 or
                (state == 0 and (job or generation or sluid)) or
                (state == 1 and (not job or generation or sluid)) or
                (state >= 2 and (not job or not generation))):
            raise ControllerUnavailable("invalid reservation status fields")
        return {"uid": uid, "request_id": request_id, "present": True, "month": month,
                "tier": ("debug", "important", "normal")[tier], "gpu": gpu, "minutes": minutes,
                "reserved_gpu_nanoseconds": amount, "scheduler_id": job or None,
                "generation": generation or None, "sluid": sluid or None,
                "state": ("unbound", "bound", "prepared", "active")[state]}
