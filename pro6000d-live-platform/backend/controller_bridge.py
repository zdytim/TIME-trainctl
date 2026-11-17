"""Staged root-broker client for the Slurm monthly quota control socket.

No direct browser access. The broker must separately authenticate the Unix
peer, authorize grants and persist approval/audit state before invoking grant.

The pidfd and peer checks reduce PID reuse races but are not an atomic proof:
exec or configuration changes can occur after verification and before receipt.
The controller protocol provides no mechanism to lock those properties across
the exchange. A successful check does not establish priority or quota activation.
"""
import os
import re
import socket
import stat
import struct
import select
import time
import sys

from activation_attestation import verify_release, _trusted_file


class ControllerUnavailable(Exception):
    pass


class PriorityBarrierWait(ControllerUnavailable):
    """A verified top-priority task is waiting; scanning may continue."""


class ControllerDenied(Exception):
    """Controller confirmed no reservation was created due to capacity."""


class ControllerOutcomeUnknown(ControllerUnavailable):
    """The controller may have applied a mutation; reconcile by grant ID."""


class ControllerBridge:
    def __init__(self, path, controller_uid, timeout=2.0, *, release_manifest=None):
        self.path = path
        self.controller_uid = int(controller_uid)
        self.timeout = timeout
        self.expected_pid = None
        self.release_manifest = release_manifest

    def _connect(self, required_pid=None):
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
                expected_pid = self.expected_pid if required_pid is None else required_pid
                if uid != self.controller_uid or pid <= 0 or (expected_pid is not None and pid != expected_pid):
                    raise ControllerUnavailable("controller peer identity mismatch")
                return sock
            except Exception:
                sock.close()
                raise
        except ControllerUnavailable:
            raise
        except (OSError, ValueError, struct.error) as exc:
            raise ControllerUnavailable("controller connection unavailable") from exc

    @staticmethod
    def _generation(pid):
        with open("/proc/%d/stat" % pid) as source:
            raw = source.read()
        end = raw.rfind(")")
        if end < 0:
            raise ValueError("invalid process stat")
        return raw[end + 2:].split()[19]

    @staticmethod
    def _evidence(pid):
        # Capture executable identity and explicit configuration inputs around
        # the heavy verifier. Root-managed file checks remain in verify_release.
        root = "/proc/%d/" % pid
        st = os.stat(root + "exe")
        with open(root + "cmdline", "rb") as f:
            args = f.read()
        with open(root + "environ", "rb") as f:
            env = f.read()
        return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size, args, env)

    def _check_process(self, fd, pid, generation):
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        if poller.poll(0) or self._generation(pid) != generation:
            raise ControllerUnavailable("controller process changed")

    def _attest_process(self, pid):
        if not self.release_manifest or not hasattr(os, "pidfd_open"):
            raise ControllerUnavailable("pidfd release attestation required")
        fd = None
        try:
            manifest, _ = _trusted_file(self.release_manifest, 0, True)
            fd = os.pidfd_open(pid, 0)
            generation = self._generation(pid)
            self._check_process(fd, pid, generation)
            evidence = self._evidence(pid)
            if verify_release(manifest, self.controller_uid, return_controller_pid=True) != pid:
                raise ControllerUnavailable("controller release verification failed")
            if self._evidence(pid) != evidence:
                raise ControllerUnavailable("controller executable or configuration changed")
            self._check_process(fd, pid, generation)
            return fd, generation
        except (OSError, ValueError, IndexError) as exc:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError as cleanup:
                    exc.pidfd_cleanup_error = cleanup
            raise ControllerUnavailable("controller identity unavailable") from exc
        except BaseException as exc:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError as cleanup:
                    exc.pidfd_cleanup_error = cleanup
            raise

    def _exchange(self, message, mutation=False, required_pid=None):
        sent = False
        pidfd = None
        try:
            if required_pid is not None:
                pidfd, generation = self._attest_process(required_pid)
            connected_at = time.monotonic()
            with (self._connect() if required_pid is None else self._connect(required_pid)) as sock:
                # The controller expects its first byte promptly after accept.
                # No hashing or release traversal occurs on the connected socket.
                if pidfd is not None:
                    self._check_process(pidfd, required_pid, generation)
                    peer = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if peer[:2] != (required_pid, self.controller_uid):
                        raise ControllerUnavailable("controller peer identity mismatch")
                    self._check_process(pidfd, required_pid, generation)
                    if time.monotonic() - connected_at >= min(self.timeout, 1.0):
                        raise ControllerUnavailable("controller identity check timed out")
                payload = message.encode("ascii") + b"\n"
                sent = True
                sock.sendall(payload)
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
        except (OSError, UnicodeError, ValueError, IndexError, struct.error, ControllerUnavailable) as exc:
            if mutation and sent:
                raise ControllerOutcomeUnknown("controller mutation outcome unknown; reconcile using the same identity and fields") from exc
            if isinstance(exc, ControllerUnavailable):
                raise
            raise ControllerUnavailable("controller request failed") from exc
        finally:
            if pidfd is not None:
                prior = sys.exc_info()[1]
                try:
                    os.close(pidfd)
                except OSError as cleanup:
                    error = (ControllerOutcomeUnknown if mutation and sent else ControllerUnavailable)(
                        "pidfd close failed%s" % (" after mutation; reconcile" if mutation and sent else ""))
                    error.pidfd_cleanup_error = cleanup
                    raise error from (prior if prior is not None else cleanup)

    @staticmethod
    def _uint(value, upper):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= upper:
            raise ValueError("invalid unsigned integer")
        return value

    def _top_identity(self, marker, uid, generation, request_id, request_hash):
        """Validate an identity for the experimental complete-marker protocol.

        The caller must set expected_pid and release_manifest, calculate the
        request hash from trusted inputs,
        and serialize submission with priority changes. A receipt alone does
        not authorize scheduling.
        """
        expected_pid = self.expected_pid
        if type(expected_pid) is not int or expected_pid <= 0:
            raise ControllerUnavailable("verified controller process required")
        uid = self._uint(uid, 2**32 - 1)
        if (not isinstance(marker, str) or not re.fullmatch(r"[0-9a-f]{32}", marker) or
                not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation) or
                not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,80}", request_id) or
                not isinstance(request_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", request_hash)):
            raise ValueError("invalid complete marker identity")
        return (marker, uid, generation, request_id, request_hash), expected_pid

    def register_top(self, marker, uid, generation, request_id, request_hash):
        identity, expected_pid = self._top_identity(marker, uid, generation, request_id, request_hash)
        marker, uid, generation, request_id, request_hash = identity
        reply = self._exchange("TOP_MARK_ADD3 %d %s %s %s %s" %
                               (uid, marker, generation, request_id, request_hash), mutation=True,
                               required_pid=expected_pid)
        expected = "OK %d %s %s %s %s" % (uid, marker, generation, request_id, request_hash)
        if reply != expected:
            raise ControllerOutcomeUnknown("unverified marker registration reply")
        return {"registered": True, "identity": identity}

    def fence_top(self):
        """Engage the experimental fence; unknown results require recovery."""
        expected_pid = self.expected_pid
        if type(expected_pid) is not int or expected_pid <= 0:
            raise ControllerUnavailable("verified controller process required")
        reply = self._exchange("TOP_FENCE1 0 1", mutation=True, required_pid=expected_pid)
        if reply != "OK":
            raise ControllerOutcomeUnknown("unverified priority fence reply")

    def cancelled_top_receipt(self, marker, uid, account, request, request_hash, scheduler_id, submit, sluid):
        """Read an existing cancellation tombstone without signalling Slurm."""
        identity, expected_pid = self._top_identity(marker, uid, account, request, request_hash)
        marker, uid, account, request, request_hash = identity
        scheduler_id = self._uint(scheduler_id, 2**32-1)
        submit = self._uint(submit, 2**64-1)
        sluid = self._uint(sluid, 2**64-1)
        if not scheduler_id or not submit or not sluid:
            raise ControllerUnavailable('invalid cancelled allocation identity')
        tail = '%d %s %s %s %s %d %d %d' % (uid, marker, account, request, request_hash, scheduler_id, submit, sluid)
        if self._exchange('TOP_CANCELLED_GET1 ' + tail, required_pid=expected_pid) != 'OK ' + tail:
            raise ControllerUnavailable('cancelled marker tombstone unavailable')
        return True

    def cancel_pending_top(self, marker, uid, account, request, request_hash, scheduler_id, submit, sluid):
        identity, pid = self._top_identity(marker, uid, account, request, request_hash)
        marker, uid, account, request, request_hash = identity
        scheduler_id = self._uint(scheduler_id, 2**32-1)
        submit = self._uint(submit, 2**64-1)
        sluid = self._uint(sluid, 2**64-1)
        if not scheduler_id or not submit or not sluid:
            raise ControllerUnavailable('invalid pending cancellation identity')
        command = 'TOP_CANCEL_PENDING1 %d %s %s %s %s %d %d %d' % (uid, marker, account, request, request_hash, scheduler_id, submit, sluid)
        if self._exchange(command, mutation=True, required_pid=pid) != 'OK':
            raise ControllerOutcomeUnknown('pending cancellation outcome unknown')

    def cancel_unbound_top(self, marker, uid, generation, request_id, request_hash):
        """Persist an unbound marker tombstone; a lost reply remains unknown."""
        identity, expected_pid = self._top_identity(marker, uid, generation, request_id, request_hash)
        marker, uid, generation, request_id, request_hash = identity
        reply = self._exchange("TOP_MARK_CANCEL4 %d %s %s %s %s" %
                               (uid, marker, generation, request_id, request_hash), mutation=True,
                               required_pid=expected_pid)
        expected = "OK %d %s %s %s %s" % (uid, marker, generation, request_id, request_hash)
        if reply != expected:
            raise ControllerOutcomeUnknown("unverified priority cancellation reply")
        return {"cancelled": True, "identity": identity}

    def bind_top(self, marker, uid, generation, request_id, request_hash, job_id):
        identity, expected_pid = self._top_identity(
            marker, uid, generation, request_id, request_hash)
        marker, uid, generation, request_id, request_hash = identity
        job_id = self._uint(job_id, 2**32 - 1)
        if not job_id:
            raise ValueError("invalid job identity")
        reply = self._exchange("TOP_MARK_BIND5 %d %s %s %s %s %d" %
                               (uid, marker, generation, request_id, request_hash, job_id), mutation=True,
                               required_pid=expected_pid)
        parts = reply.split(" ")
        prefix = ["OK", str(uid), marker, generation, request_id, request_hash, str(job_id)]
        if (len(parts) != 11 or parts[:7] != prefix or parts[9:] != ["0", "0"] or
                any(not re.fullmatch(r"[1-9][0-9]*", value) or int(value) > 2**64-1
                    for value in parts[7:9])):
            raise ControllerOutcomeUnknown("unverified marker binding reply")
        return {"job_id": job_id, "submit": int(parts[7]), "sluid": int(parts[8]),
                "restart": 0, "generation": 0}

    def supports_creation_bound_top(self, epoch, boot, watermark, digest, uid, marker):
        """Attest capability and the exact freshly audited controller snapshot."""
        if (type(self.expected_pid) is not int or self.expected_pid <= 0 or
                not re.fullmatch(r'[0-9a-f]{32}', epoch) or
                not re.fullmatch(r'[0-9a-f]{32}', boot) or
                type(watermark) is not int or not 0 <= watermark <= 2**64-1 or
                not re.fullmatch(r'[0-9a-f]{64}', digest) or
                type(uid) is not int or not 0 <= uid <= 2**32-1 or
                not re.fullmatch(r'[0-9a-f]{32}', marker)):
            raise ControllerUnavailable('invalid capability snapshot identity')
        command = 'TOP_FULL_PATH_CAP1 0 %s %s %d %s %d %s' % (epoch, boot, watermark, digest, uid, marker)
        return self._exchange(command, required_pid=self.expected_pid) == 'OK CREATION_BOUND_TOP1'

    def supports_top_uncertainty_clear(self):
        if type(self.expected_pid) is not int or self.expected_pid <= 0:
            raise ControllerUnavailable('verified controller process required')
        return self._exchange('TOP_CLEAR_CAP1 0', required_pid=self.expected_pid) == 'OK SNAPSHOT_CLEAR2'

    def top_snapshot_digest(self, epoch, boot, watermark):
        if (not isinstance(epoch, str) or not re.fullmatch(r'[0-9a-f]{32}', epoch) or
                not isinstance(boot, str) or not re.fullmatch(r'[0-9a-f]{32}', boot) or
                type(watermark) is not int or not 0 <= watermark <= 2**64-1 or
                type(self.expected_pid) is not int or self.expected_pid <= 0):
            raise ControllerUnavailable('invalid priority snapshot identity')
        reply = self._exchange('TOP_SNAPSHOT1 0 %s %s %d' % (epoch, boot, watermark),
                               required_pid=self.expected_pid)
        parts = reply.split(' ')
        if len(parts) != 2 or parts[0] != 'OK' or not re.fullmatch(r'[0-9a-f]{64}', parts[1]):
            raise ControllerUnavailable('controller priority snapshot unavailable')
        return parts[1]

    def clear_top_uncertainty(self, epoch, boot, watermark, ledger_seq, digest):
        if (not isinstance(epoch, str) or not re.fullmatch(r'[0-9a-f]{32}', epoch) or
                not isinstance(boot, str) or not re.fullmatch(r'[0-9a-f]{32}', boot) or
                type(watermark) is not int or not 0 <= watermark <= 2**64-1 or
                type(ledger_seq) is not int or not 0 <= ledger_seq <= 2**64-1 or
                not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or
                type(self.expected_pid) is not int or self.expected_pid <= 0):
            raise ControllerUnavailable('invalid uncertainty clear identity')
        tail = '%s %s %d %d %s' % (epoch, boot, watermark, ledger_seq, digest)
        if self._exchange('TOP_UNCERTAIN_CLEAR2 0 ' + tail, mutation=True,
                          required_pid=self.expected_pid) != 'OK ' + tail:
            raise ControllerOutcomeUnknown('uncertainty clear outcome unknown')

    def release_top(self, marker, uid, generation, request_id, request_hash, job_id, submit, sluid):
        """Release only the controller-verified held job; unknown means reconcile.

        A successful receipt does not prove the task has started or that the
        ordinary-user allocation barrier has been cleared.
        """
        identity, expected_pid = self._top_identity(marker, uid, generation, request_id, request_hash)
        marker, uid, generation, request_id, request_hash = identity
        job_id = self._uint(job_id, 2**32 - 1)
        submit = self._uint(submit, 2**64 - 1)
        sluid = self._uint(sluid, 2**64 - 1)
        if not job_id or not submit or not sluid:
            raise ValueError('invalid held job identity')
        tail = '%d %s %s %s %s %d %d %d' % (uid, marker, generation, request_id,
                                             request_hash, job_id, submit, sluid)
        reply = self._exchange('TOP_MARK_RELEASE1 ' + tail, mutation=True, required_pid=expected_pid)
        if reply != 'OK ' + tail:
            raise ControllerOutcomeUnknown('unverified held job release reply')
        return True

    def readiness(self):
        """Verify the paired controller protocol and recovered gate state."""
        return self._exchange("READY1 0") == "OK TIME26_SELECTABLE_ENDING2_ROLL1_RANK1_CANCEL_BOUND1_AUTOCANCEL1_RECONCILE1_NODEPROOF1_SELECT_DONE1_GPUMAP1_CANCEL_EMPTY1"

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
        if line == "DENIED capacity":
            raise ControllerDenied("selected tier cannot cover the request")
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

    def cancel_bound_reservation(self, uid, request_id, scheduler_id):
        """Release a controller-verified terminal job that never allocated."""
        uid, request_id = self._reservation_request(uid, request_id)
        scheduler_id = self._uint(scheduler_id, 2**32 - 1)
        if not scheduler_id:
            raise ValueError("invalid scheduler job ID")
        line = self._exchange("CANCEL_BOUND1 %d %s %d" % (uid, request_id, scheduler_id), mutation=True)
        if line != "OK":
            raise ControllerOutcomeUnknown("bound cancellation outcome unknown")
        return {"cancelled": True}

    def rollover_reservation(self, uid, request_id):
        """Renew an unstarted hold into the controller's current month."""
        uid, request_id = self._reservation_request(uid, request_id)
        line = self._exchange("ROLL1 %d %s" % (uid, request_id), mutation=True)
        if line == "DENIED capacity":
            raise ControllerDenied("capacity")
        if line != "OK":
            raise ControllerOutcomeUnknown("reservation rollover outcome unknown")
        return {"rolled": True}

    def bind_reservation(self, uid, request_id, scheduler_id):
        """Bind and return the controller's durable initial Slurm identity."""
        uid, request_id = self._reservation_request(uid, request_id)
        scheduler_id = self._uint(scheduler_id, 2**32 - 1)
        if not scheduler_id:
            raise ValueError("invalid scheduler job ID")
        parts = self._exchange("BIND2 %d %s %d" % (uid, request_id, scheduler_id), mutation=True).split(" ")
        if (len(parts) != 6 or parts[:4] != ["OK", str(uid), request_id, str(scheduler_id)] or
                any(not re.fullmatch(r"[1-9][0-9]*", x) or int(x) > 2**64-1 for x in parts[4:])):
            raise ControllerOutcomeUnknown("unverified reservation binding receipt")
        return {"request_id": request_id, "scheduler_id": scheduler_id,
                "submit": int(parts[4]), "sluid": int(parts[5])}

    def lookup_top_submission(self, marker, uid, account, request, request_hash):
        identity, pid = self._top_identity(marker, uid, account, request, request_hash)
        marker, uid, account, request, request_hash = identity
        fields = self._exchange('TOP_LOOKUP1 %d %s %s %s %s' % (uid, marker, account, request, request_hash),
                                required_pid=pid).split(' ')
        if (len(fields) != 5 or fields[0] != 'OK' or
                any(not re.fullmatch(r'(?:0|[1-9][0-9]*)', value) or int(value) > 2**64-1 for value in fields[1:])):
            raise ControllerUnavailable('priority submission identity unavailable')
        job, submit, sluid, generation = map(int, fields[1:])
        if not 0 < job <= 2**32-1 or not 0 < submit <= 2**63-1 or not sluid or generation > 2**63-1:
            raise ControllerUnavailable('invalid priority submission identity')
        return job, submit, sluid, generation

    def top_pending_receipt(self, uid, marker, scheduler_id):
        uid, marker = self._reservation_request(uid, marker)
        scheduler_id = self._uint(scheduler_id, 2**32-1)
        if not scheduler_id or type(self.expected_pid) is not int or self.expected_pid <= 0:
            raise ControllerUnavailable('verified pending identity required')
        fields = self._exchange('TOP_PENDING_GET1 %d %s %d' % (uid, marker, scheduler_id),
                                required_pid=self.expected_pid).split(' ')
        if (len(fields) != 6 or fields[:4] != ['OK', str(uid), marker, str(scheduler_id)] or
                any(not re.fullmatch(r'[1-9][0-9]*', x) or int(x) > 2**64-1 for x in fields[4:])):
            raise ControllerUnavailable('pending marker receipt unavailable')
        return {'job_id': scheduler_id, 'submit': int(fields[4]), 'sluid': int(fields[5]),
                'restart': 0, 'generation': 0}

    def top_terminal_receipt(self, uid, marker, scheduler_id):
        """Read the controller's settled allocation identity and outcome."""
        uid, marker = self._reservation_request(uid, marker)
        scheduler_id = self._uint(scheduler_id, 2**32-1)
        if not scheduler_id or type(self.expected_pid) is not int or self.expected_pid <= 0:
            raise ControllerUnavailable('verified terminal identity required')
        fields = self._exchange('TOP_TERMINAL_GET1 %d %s %d' % (uid, marker, scheduler_id),
                                required_pid=self.expected_pid).split(' ')
        if (len(fields) != 9 or fields[:4] != ['OK', str(uid), marker, str(scheduler_id)] or
                any(not re.fullmatch(r'(?:0|[1-9][0-9]*)', x) or int(x) > 2**64-1 for x in fields[4:])):
            raise ControllerUnavailable('terminal marker receipt unavailable')
        submit, sluid, generation, state, exit_code = map(int, fields[4:])
        if not submit or not sluid or not generation or state > 2**32-1 or exit_code > 2**32-1:
            raise ControllerUnavailable('invalid terminal receipt')
        return {'submit': submit, 'sluid': sluid, 'generation': generation, 'state': state, 'exit_code': exit_code}

    def top_running_receipt(self, uid, marker, scheduler_id):
        uid, marker = self._reservation_request(uid, marker)
        scheduler_id = self._uint(scheduler_id, 2**32-1)
        if not scheduler_id or type(self.expected_pid) is not int or self.expected_pid <= 0:
            raise ControllerUnavailable('verified running identity required')
        fields = self._exchange('TOP_RUNNING_GET1 %d %s %d' % (uid, marker, scheduler_id),
                                required_pid=self.expected_pid).split(' ')
        if (len(fields) != 7 or fields[:4] != ['OK', str(uid), marker, str(scheduler_id)] or
                any(not re.fullmatch(r'[1-9][0-9]*', x) or int(x) > 2**64-1 for x in fields[4:])):
            raise ControllerUnavailable('running marker receipt unavailable')
        return {'submit': int(fields[4]), 'sluid': int(fields[5]), 'generation': int(fields[6])}

    def legacy_terminal_receipt(self, uid, request_id, scheduler_id):
        uid, request_id = self._reservation_request(uid, request_id)
        scheduler_id = self._uint(scheduler_id, 2**32-1)
        if not scheduler_id or type(self.expected_pid) is not int or self.expected_pid <= 0:
            raise ControllerUnavailable('verified controller required')
        fields = self._exchange('LEGACY_TERMINAL_GET1 %d %s %d' % (uid, request_id, scheduler_id), required_pid=self.expected_pid).split(' ')
        if (len(fields) != 8 or fields[:5] != ['OK', '1', str(uid), request_id, str(scheduler_id)] or
                any(not re.fullmatch(r'(?:0|[1-9][0-9]*)', x) or int(x) > 2**64-1 for x in fields[5:])):
            raise ControllerUnavailable('legacy terminal receipt unavailable')
        generation, state, exit_code = map(int, fields[5:])
        if not generation or state > 2**32-1 or exit_code > 2**32-1:
            raise ControllerUnavailable('legacy terminal receipt invalid')
        return {'generation': generation, 'state': state, 'exit_code': exit_code}

    def ordinary_terminal_receipt(self, uid, request_id, scheduler_id):
        uid, request_id = self._reservation_request(uid, request_id)
        scheduler_id = self._uint(scheduler_id, 2**32-1)
        if not scheduler_id or type(self.expected_pid) is not int or self.expected_pid <= 0:
            raise ControllerUnavailable('verified terminal identity required')
        fields = self._exchange('ORD_TERMINAL_GET1 %d %s %d' % (uid, request_id, scheduler_id),
                                required_pid=self.expected_pid).split(' ')
        if (len(fields) != 9 or fields[:4] != ['OK', str(uid), request_id, str(scheduler_id)] or
                any(not re.fullmatch(r'(?:0|[1-9][0-9]*)', x) or int(x) > 2**64-1 for x in fields[4:])):
            raise ControllerUnavailable('ordinary terminal receipt unavailable')
        submit, sluid, generation, state, exit_code = map(int, fields[4:])
        if not submit or not sluid or not generation or state > 2**32-1 or exit_code > 2**32-1:
            raise ControllerUnavailable('ordinary terminal receipt invalid')
        return {'submit': submit, 'sluid': sluid, 'generation': generation, 'state': state, 'exit_code': exit_code}

    def ordinary_running_receipt(self, uid, request_id, scheduler_id):
        uid, request_id = self._reservation_request(uid, request_id)
        scheduler_id = self._uint(scheduler_id, 2**32-1)
        if not scheduler_id or type(self.expected_pid) is not int or self.expected_pid <= 0:
            raise ControllerUnavailable('verified running identity required')
        fields = self._exchange('ORD_RUNNING_GET1 %d %s %d' % (uid, request_id, scheduler_id),
                                required_pid=self.expected_pid).split(' ')
        if (len(fields) != 7 or fields[:4] != ['OK', str(uid), request_id, str(scheduler_id)] or
                any(not re.fullmatch(r'[1-9][0-9]*', x) or int(x) > 2**64-1 for x in fields[4:])):
            raise ControllerUnavailable('ordinary running identity unavailable')
        return {'submit': int(fields[4]), 'sluid': int(fields[5]), 'generation': int(fields[6])}

    def binding_receipt(self, uid, request_id, scheduler_id):
        """Read the controller's durable BIND2 identity for this live job."""
        uid, request_id = self._reservation_request(uid, request_id)
        scheduler_id = self._uint(scheduler_id, 2**32 - 1)
        if not scheduler_id or type(self.expected_pid) is not int or self.expected_pid <= 0:
            raise ControllerUnavailable('verified controller and job required')
        fields = self._exchange('BIND2_GET %d %s %d' % (uid, request_id, scheduler_id),
                                required_pid=self.expected_pid).split(' ')
        if (len(fields) != 6 or fields[:4] != ['OK', str(uid), request_id, str(scheduler_id)] or
                any(not re.fullmatch(r'[1-9][0-9]*', x) or int(x) > 2**64-1 for x in fields[4:])):
            raise ControllerUnavailable('reservation identity not confirmed')
        return {'submit': int(fields[4]), 'sluid': int(fields[5])}

    def pending_binding_receipt(self, uid, request_id, scheduler_id):
        """Read BIND2 only if the exact Slurm job is currently pending."""
        uid, request_id = self._reservation_request(uid, request_id)
        scheduler_id = self._uint(scheduler_id, 2**32 - 1)
        if not scheduler_id or type(self.expected_pid) is not int or self.expected_pid <= 0:
            raise ControllerUnavailable('verified controller and job required')
        fields = self._exchange('BIND2_PENDING_GET1 %d %s %d' % (uid, request_id, scheduler_id),
                                required_pid=self.expected_pid).split(' ')
        if (len(fields) != 6 or fields[:4] != ['OK', str(uid), request_id, str(scheduler_id)] or
                any(not re.fullmatch(r'[1-9][0-9]*', x) or int(x) > 2**64-1 for x in fields[4:])):
            raise ControllerUnavailable('pending reservation identity not confirmed')
        return {'submit': int(fields[4]), 'sluid': int(fields[5])}

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
                job > 2**32 - 1 or generation > 2**64 - 1 or sluid > 2**64 - 1 or state > 6 or
                (state == 0 and (job or generation or sluid)) or
                (state == 1 and (not job or generation or sluid)) or
                (state in (2, 3, 5) and (not job or not generation)) or
                (state == 4 and (job or generation or sluid)) or
                (state == 6 and (not job or generation or sluid))):
            raise ControllerUnavailable("invalid reservation status fields")
        return {"uid": uid, "request_id": request_id, "present": True, "month": month,
                "tier": ("debug", "important", "normal")[tier], "gpu": gpu, "minutes": minutes,
                "original_gpu_nanoseconds": amount, "hold_active": state < 4, "scheduler_id": job or None,
                "generation": generation or None, "sluid": sluid or None,
                "state": ("unbound", "bound", "prepared", "active", "cancelled", "released", "cancelled_bound")[state]}

    def cancel_request(self, uid, request_id, tier, gpu, minutes, *, required_pid=None):
        """Atomically cancel an unbound hold or tombstone an absent request."""
        uid, request_id = self._reservation_request(uid, request_id)
        tiers = {"debug": 0, "important": 1, "normal": 2}
        if tier not in tiers:
            raise ValueError("invalid reservation tier")
        gpu = self._uint(gpu, 2**32 - 1)
        minutes = self._uint(minutes, 2**32 - 1)
        if not gpu or not minutes or gpu * minutes * 60 * 10**9 > 2**64 - 1:
            raise ValueError("invalid reservation amount")
        command = "CANCEL_REQUEST1 %d %s %d %d %d" % (uid, request_id, tiers[tier], gpu, minutes)
        if required_pid is not None:
            if type(required_pid) is not int or required_pid <= 0 or self.expected_pid != required_pid:
                raise ControllerUnavailable('verified controller process required')
            line = self._exchange(command, mutation=True, required_pid=required_pid)
        else:
            line = self._exchange(command, mutation=True)
        if line != "OK":
            raise ControllerOutcomeUnknown("cancellation outcome unknown; reconcile same identity")
        return {"request_id": request_id, "cancelled": True}

    def cancel_unbound_reservation(self, uid, request_id):
        """Cancel only an observed unbound hold; reconcile every unknown result.

        The controller rechecks binding atomically. The caller must separately
        prove that no scheduler submission is in flight before invoking this.
        """
        uid, request_id = self._reservation_request(uid, request_id)
        current = self.reservation_status(uid, request_id)
        if not current["present"]:
            raise ControllerOutcomeUnknown("reservation absent; prior submission outcome is not established")
        if current["state"] == "cancelled":
            return {"request_id": request_id, "cancelled": True}
        if current["state"] != "unbound":
            raise ControllerUnavailable("only an unbound reservation may be cancelled")
        line = self._exchange("CANCEL1 %d %s" % (uid, request_id), mutation=True)
        if line != "OK":
            raise ControllerOutcomeUnknown("unexpected cancellation reply; reconcile the same request ID")
        return {"request_id": request_id, "cancelled": True}


class PinnedControllerBridge(ControllerBridge):
    """Experimental per-exchange attestation for a fixed controller peer.

    A pidfd check on each exchange does not itself establish a stable controller
    boot across several exchanges. Callers must also verify boot identity and
    reconcile uncertain mutations before using a result to authorize work.
    """
    def __init__(self, path, controller_uid, expected_pid, timeout=2.0, *, release_manifest):
        if (type(expected_pid) is not int or expected_pid <= 0 or
                not isinstance(release_manifest, str) or not release_manifest):
            raise ValueError('attested controller identity required')
        super().__init__(path, controller_uid, timeout, release_manifest=release_manifest)
        self.expected_pid = expected_pid
        self._pinned_identity = (path, self.controller_uid, expected_pid, release_manifest)

    def _exchange(self, message, mutation=False, required_pid=None):
        path, uid, pid, manifest = self._pinned_identity
        if ((self.path, self.controller_uid, self.expected_pid, self.release_manifest) != self._pinned_identity or
                (required_pid is not None and required_pid != pid)):
            raise ControllerUnavailable('controller peer identity changed')
        return super()._exchange(message, mutation=mutation, required_pid=pid)
