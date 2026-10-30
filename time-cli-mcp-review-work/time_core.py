#!/usr/bin/env python3
"""Shared user-scoped client for the existing time/pro6000d broker API.

This module deliberately contains no Slurm, SQLite, sudo, UID override, or
administrator gateway code.  The broker obtains the caller's real UID from
the Unix socket peer credentials.
"""

from __future__ import print_function

import datetime as _datetime
import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import socket
import stat
import tempfile
import time
import uuid


SCHEMA_VERSION = "1.0"
CLIENT_VERSION = "0.1.0"
DEFAULT_SOCKET = "/run/pro6000d-training/backend.sock"
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class TimeError(Exception):
    """An expected client or broker failure."""

    def __init__(self, code, message, status=None, details=None,
                 retryable=False, outcome="rejected", unknown=False):
        super(TimeError, self).__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details
        self.retryable = bool(retryable)
        self.outcome = "unknown" if unknown else outcome
        self.unknown = bool(unknown)


class UnknownOutcome(TimeError):
    def __init__(self, message, details=None):
        super(UnknownOutcome, self).__init__(
            "submission_outcome_unknown", message, status=None,
            details=details, retryable=False, unknown=True)


class ServiceUnavailable(TimeError):
    def __init__(self, message, details=None):
        super(ServiceUnavailable, self).__init__(
            "backend_unavailable", message, status=503,
            details=details, retryable=True, outcome="rejected")


class _MalformedResponse(Exception):
    """The broker response cannot establish a mutation outcome."""


def utc_now():
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def request_hash(method, path, body):
    return hashlib.sha256(_json_bytes({
        "method": method.upper(), "path": path, "body": body or {}
    })).hexdigest()


def shell_quote_argv(argv):
    """Encode argv for the legacy server's Bash command field.

    The server still executes `bash -lc command`; this function preserves the
    argv values through that shell boundary but cannot turn the old API into an
    execve/native-argv API.
    """
    if not argv:
        raise ValueError("command cannot be empty")
    return " ".join(shlex.quote(str(item)) for item in argv)


def parse_memory(value):
    if isinstance(value, bool):
        raise ValueError("memory must be an integer or a size such as 32GiB")
    text = str(value).strip()
    match = re.fullmatch(r"([0-9]+)\s*(MiB|GiB|M|G|MB|GB)?", text,
                         re.IGNORECASE)
    if not match:
        raise ValueError("invalid memory: %s" % value)
    amount = int(match.group(1))
    suffix = (match.group(2) or "MiB").lower()
    if suffix in ("g", "gb", "gib"):
        amount *= 1024
    if amount < 1:
        raise ValueError("memory must be positive")
    return amount


def parse_duration(value):
    if isinstance(value, bool):
        raise ValueError("time must be an integer or a duration such as 12h")
    text = str(value).strip().lower()
    match = re.fullmatch(r"([0-9]+)\s*(m|min|h|hr|d)?", text)
    if not match:
        raise ValueError("invalid time: %s" % value)
    amount = int(match.group(1))
    suffix = match.group(2) or "m"
    if suffix in ("h", "hr"):
        amount *= 60
    elif suffix == "d":
        amount *= 24 * 60
    if amount < 1:
        raise ValueError("time must be positive")
    return amount


def normalize_command_job(command, name=None, gpus=1, cpus=1, memory="1GiB",
                           duration="60m", qos=None, workdir=None,
                           output=None, environment="system"):
    if not command:
        raise ValueError("a command is required after --")
    try:
        gpu_count = int(gpus)
        cpu_count = int(cpus)
    except (TypeError, ValueError):
        raise ValueError("gpus and cpus must be integers")
    if gpu_count < 1 or cpu_count < 1:
        raise ValueError("gpus and cpus must be positive")
    command_name = name or os.path.basename(str(command[0])) or "timectl"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", command_name):
        raise ValueError("name must contain 1-64 safe characters")
    effective_workdir = os.path.abspath(workdir or os.getcwd())
    result = {
        "name": command_name,
        "command": shell_quote_argv(command),
        "gpu": gpu_count,
        "cpu": cpu_count,
        "memory_mb": parse_memory(memory),
        "time_minutes": parse_duration(duration),
        "workdir": effective_workdir,
        "environment": environment,
    }
    if qos:
        result["qos"] = qos
    if output:
        result["output_path"] = os.path.abspath(output)
    return result


def _secure_mode(path, mode):
    try:
        os.chmod(str(path), mode)
    except OSError:
        pass


class RequestLedger(object):
    """Small user-owned durable ledger for mutation request IDs."""

    def __init__(self, state_dir=None):
        candidate = state_dir or os.environ.get("TIME_STATE_DIR")
        if candidate:
            self.directory = pathlib.Path(os.path.expanduser(candidate))
        else:
            base = os.environ.get("XDG_STATE_HOME")
            if base:
                self.directory = pathlib.Path(base).expanduser() / "time"
            else:
                self.directory = pathlib.Path.home() / ".local" / "state" / "time"
        self.path = self.directory / "requests.json"
        self.lock_path = self.directory / "requests.lock"
        self.uid = os.getuid()
        self._ensure_directory()

    @staticmethod
    def _assert_no_symlink_components(path):
        current = pathlib.Path(os.path.abspath(str(path))).anchor
        for part in pathlib.Path(os.path.abspath(str(path))).parts[1:]:
            current = os.path.join(current, part)
            try:
                component = os.lstat(current)
            except FileNotFoundError:
                break
            if stat.S_ISLNK(component.st_mode):
                raise TimeError("request_ledger_unsecure",
                                "local request state path contains a symlink")

    def _check_owned_regular(self, path, mode, label):
        try:
            value = os.lstat(str(path))
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
            raise TimeError("request_ledger_unsecure",
                            "%s must be a regular file, not a symlink" % label)
        if value.st_uid != self.uid:
            raise TimeError("request_ledger_unsecure",
                            "%s is not owned by the current user" % label)
        if value.st_mode & 0o077:
            os.chmod(str(path), mode)
        return True

    def _ensure_directory(self):
        self._assert_no_symlink_components(self.directory)
        if self.directory.exists() or self.directory.is_symlink():
            directory_stat = os.lstat(str(self.directory))
            if not stat.S_ISDIR(directory_stat.st_mode) or stat.S_ISLNK(directory_stat.st_mode):
                raise TimeError("request_ledger_unsecure",
                                "local request state path is not a real directory")
            if directory_stat.st_uid != self.uid:
                raise TimeError("request_ledger_unsecure",
                                "local request state directory is not user-owned")
        self.directory.mkdir(parents=True, exist_ok=True)
        directory_stat = os.lstat(str(self.directory))
        if not stat.S_ISDIR(directory_stat.st_mode) or stat.S_ISLNK(directory_stat.st_mode):
            raise TimeError("request_ledger_unsecure",
                            "local request state path is not a real directory")
        if directory_stat.st_uid != self.uid:
            raise TimeError("request_ledger_unsecure",
                            "local request state directory is not user-owned")
        os.chmod(str(self.directory), 0o700)

    def _read_unlocked(self):
        fd = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(str(self.path), flags)
            value = os.fstat(fd)
            if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
                raise TimeError("request_ledger_unsecure",
                                "request ledger must be a regular non-symlink file")
            if value.st_uid != self.uid:
                raise TimeError("request_ledger_unsecure",
                                "request ledger is not owned by the current user")
            if value.st_mode & 0o077:
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = None
                value = json.load(stream)
            if not isinstance(value, dict) or not isinstance(value.get("requests", {}), dict):
                raise ValueError("invalid ledger")
            return value
        except FileNotFoundError:
            return {"version": 1, "requests": {}}
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise TimeError("request_ledger_unreadable",
                            "local request ledger cannot be read",
                            details=str(exc), retryable=False)
        finally:
            if fd is not None:
                os.close(fd)

    def _write_unlocked(self, value):
        fd, temporary = tempfile.mkstemp(prefix="requests.", dir=str(self.directory))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, sort_keys=True,
                          indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, str(self.path))
            self._check_owned_regular(self.path, 0o600, "request ledger")
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @contextlib.contextmanager
    def _locked(self):
        """Serialize all ledger read/modify/write transactions per user."""
        self._ensure_directory()
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(str(self.lock_path), flags, 0o600)
        except OSError as exc:
            raise TimeError("request_ledger_unsecure",
                            "cannot open the per-user request lock",
                            details=str(exc))
        try:
            value = os.fstat(fd)
            if not stat.S_ISREG(value.st_mode) or value.st_uid != self.uid:
                raise TimeError("request_ledger_unsecure",
                                "per-user request lock is not a regular user-owned file")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def new_id(self):
        return "req-" + uuid.uuid4().hex

    def reserve(self, request_id, method, path, body):
        if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", request_id or ""):
            raise ValueError("request ID must contain 8-80 letters, digits, or hyphens")
        digest = request_hash(method, path, body)
        with self._locked():
            ledger = self._read_unlocked()
            prior = ledger["requests"].get(request_id)
            if prior:
                if prior.get("request_hash") != digest:
                    raise TimeError("idempotency_conflict",
                                    "request ID is already bound to different request body",
                                    status=409)
                if request_hash(prior.get("method", ""), prior.get("path", ""),
                                prior.get("body", {})) != prior.get("request_hash"):
                    raise TimeError("request_ledger_corrupt",
                                    "request ID record was modified and cannot be reused")
                # The legacy broker does not guarantee idempotency for every
                # mutation. A prior attempt may have succeeded even when its
                # response was lost: never resend it automatically.
                raise TimeError("request_already_attempted",
                                "request ID was already attempted; inspect it with timectl request-status before taking further action",
                                status=409, details={"request_id": request_id,
                                                     "outcome": prior.get("outcome", "unknown")})
            else:
                ledger["requests"][request_id] = {
                    "request_id": request_id,
                    "request_hash": digest,
                    "method": method.upper(),
                    "path": path,
                    "body": body or {},
                    "created_at": utc_now(),
                    "updated_at": utc_now(),
                    "outcome": "pending",
                }
                self._write_unlocked(ledger)
        return digest

    def update(self, request_id, **fields):
        with self._locked():
            ledger = self._read_unlocked()
            record = ledger["requests"].get(request_id)
            if not record:
                return
            record.update(fields)
            record["updated_at"] = utc_now()
            self._write_unlocked(ledger)

    def get(self, request_id):
        with self._locked():
            return self._read_unlocked()["requests"].get(request_id)

    def all(self):
        with self._locked():
            return list(self._read_unlocked()["requests"].values())


class BrokerClient(object):
    def __init__(self, socket_path=None):
        if socket_path is not None and socket_path != DEFAULT_SOCKET:
            raise ValueError("custom broker socket is not supported")
        self.socket_path = DEFAULT_SOCKET

    def request(self, method, path, body=None, timeout=10, mutation=False):
        request = {"method": method.upper(), "path": path, "body": body or {}}
        encoded = (_json_bytes(request) + b"\n")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise TimeError("request_too_large", "request exceeds 1 MiB",
                            status=413)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sent = False
        try:
            sock.connect(self.socket_path)
            sock.sendall(encoded)
            sent = True
            chunks = []
            total = 0
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise _MalformedResponse("backend response exceeds 8 MiB")
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
            raw = b"".join(chunks).split(b"\n", 1)[0]
            if not raw:
                raise _MalformedResponse("backend returned no response")
            try:
                response = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise _MalformedResponse("backend returned invalid JSON: %s" % exc)
        except _MalformedResponse as exc:
            if mutation:
                raise UnknownOutcome(
                    "broker response was incomplete or malformed; the mutation may still be processing",
                    details=str(exc))
            raise ServiceUnavailable(str(exc))
        except TimeError:
            raise
        except (OSError, socket.timeout) as exc:
            if mutation and sent:
                raise UnknownOutcome(
                    "broker response was not received; the mutation may still be processing",
                    details=str(exc))
            if mutation:
                raise UnknownOutcome(
                    "mutation could not be confirmed; inspect the request ID before retrying",
                    details=str(exc))
            raise ServiceUnavailable("cannot reach time backend", details=str(exc))
        finally:
            sock.close()
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            if mutation:
                raise UnknownOutcome(
                    "broker response was not a valid outcome envelope; the mutation may still be processing")
            raise ServiceUnavailable("backend response is not a valid outcome envelope")
        if not response.get("ok"):
            error = response.get("error") or {}
            if (not isinstance(error, dict) or
                    not isinstance(error.get("code"), str) or
                    not isinstance(error.get("message"), str)):
                if mutation:
                    raise UnknownOutcome(
                        "broker returned a malformed rejection envelope; the mutation may still be processing")
                raise ServiceUnavailable("backend returned a malformed rejection envelope")
            status = error.get("status")
            code = error.get("code") or "backend_error"
            message = error.get("message") or "backend rejected request"
            retryable = status in (408, 429, 502, 503, 504)
            server_outcome = str(error.get("outcome") or "").lower()
            # An internal/timeout response can be produced after a commit;
            # only a well-formed, definite client rejection is conclusive.
            unknown = (code in ("submission_uncertain", "completion_unknown") or
                       server_outcome == "unknown" or
                       (mutation and isinstance(status, int) and (status >= 500 or status == 408)))
            if unknown:
                retryable = False  # Query the saved request ID before any retry.
            raise TimeError(code, message, status=status,
                            details=error.get("details"),
                            retryable=retryable,
                            outcome="unknown" if unknown else (server_outcome or "rejected"),
                            unknown=unknown)
        return response.get("data", {})


def response_envelope(ok, data=None, error=None, request_id=None,
                      outcome=None, server=None, warnings=None):
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": bool(ok),
        "data": data if ok else None,
        "error": error if not ok else None,
        "warnings": warnings or [],
        "meta": {
            "client_version": CLIENT_VERSION,
            "request_id": request_id,
            "observed_at": utc_now(),
            "outcome": outcome or ("confirmed" if ok else "rejected"),
            "server_envelope": server or "legacy-1.0",
        },
    }


def error_envelope(exc, request_id=None):
    return response_envelope(False, request_id=request_id, outcome=exc.outcome,
                             error={
                                 "code": exc.code,
                                 "message": exc.message,
                                 "status": exc.status,
                                 "details": exc.details,
                                 "retryable": exc.retryable,
                                 "outcome": exc.outcome,
                             })


def job_outcome(job, mutation):
    if not mutation:
        return "confirmed"
    state = str((job or {}).get("state", "")).upper()
    reason = str((job or {}).get("reason_code", "")).lower()
    if reason == "cancellation_requested" and state not in (
            "CANCELLED", "CANCELED", "COMPLETED", "FAILED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY"):
        return "accepted_pending"
    if state == "SUBMITTING" or "uncertain" in reason or state == "COMPLETION_UNKNOWN":
        return "unknown"
    if state == "CANCELLATION_REQUESTED":
        return "accepted_pending"
    if state == "REJECTED":
        return "rejected"
    return "confirmed"


def format_error(exc, request_id=None):
    return error_envelope(exc, request_id=request_id)


def discover_server_capabilities(client):
    """Discover the broker contract without inferring enforcement from config."""
    try:
        data = client.request("GET", "/api/v1/capabilities", timeout=10, mutation=False)
        if not isinstance(data, dict) or data.get("schema_version") != "1.0":
            return {"state": "unknown", "route": "GET /api/v1/capabilities",
                    "reason": "unsupported or malformed capability schema"}
        return {"state": "reachable", "route": "GET /api/v1/capabilities",
                "capabilities": data}
    except TimeError as exc:
        if exc.status != 404:
            return {"state": "unknown", "route": "GET /api/v1/capabilities",
                    "error": {"code": exc.code, "message": exc.message}}
    try:
        identity = client.request("GET", "/api/v1/me", timeout=10, mutation=False)
        return {"state": "legacy", "route": "GET /api/v1/me",
                "identity_version": identity.get("version") if isinstance(identity, dict) else None}
    except TimeError as exc:
        return {"state": "unreachable", "route": "GET /api/v1/me",
                "error": {"code": exc.code, "message": exc.message}}


def feature_capabilities(discovery=None):
    """Separate client support, reviewed evidence, and runtime server discovery."""
    discovery = discovery or {"state": "unknown", "reason": "not probed"}
    route = lambda value: {"server_state": "unknown", "reviewed_route": value}
    observed = discovery.get("capabilities") if isinstance(discovery, dict) else None
    observed_monthly = observed.get("monthly_quota") if isinstance(observed, dict) else None
    monthly = {
        "server_state": "unknown", "enforcement_ready": "unknown",
        "client_support": "unsupported", "accounting": "unknown",
        "reason": "no authoritative runtime capability observed",
    }
    if isinstance(observed_monthly, dict) and observed_monthly.get("enforcement_ready") is False:
        mode = observed_monthly.get("mode")
        if mode in ("disabled", "staged"):
            monthly.update(server_state=mode, enforcement_ready=False,
                           accounting=observed_monthly.get("accounting", "unknown"),
                           reason="server reports the monthly gate is not enabled")
    return {
        "source": "reviewed_snapshot_plus_runtime_discovery",
        "server_discovery": discovery,
        "server_capabilities_endpoint": {"server_state": "observed" if isinstance(observed, dict) else "unknown", "route": "GET /api/v1/capabilities"},
        "commands": {
            "identity": dict(route("GET /api/v1/me"), client_support="read"),
            "state": dict(route("GET /api/v1/state"), client_support="read"),
            "queue": dict(route("GET /api/v1/jobs"), client_support="read"),
            "validate": dict(route("POST /api/v1/jobs/validate"), client_support="read"),
            "submit": dict(route("POST /api/v1/jobs"), client_support="write"),
            "cancel": dict(route("POST /api/v1/jobs/{id}/cancel"), client_support="write"),
            "logs_tail": dict(route("GET /api/v1/jobs/{id}/log"), client_support="read", max_bytes=1048576),
            "logs_follow": {"server_state": "unknown", "reviewed_route": None, "client_support": "unsupported", "reason": "no follow contract"},
            "native_argv": {"server_state": "unknown", "reviewed_route": None, "client_support": "unsupported", "reason": "legacy shell command boundary"},
            "multi_node": {"server_state": "unknown", "reviewed_route": None, "client_support": "unsupported", "reason": "no authoritative multi-node contract"},
            "applications": dict(route("GET/POST /api/v1/applications"), client_support="read/write"),
            "admin_preview_apply": dict(route("existing preview/apply routes"), client_support="admin write"),
            "audit": dict(route("GET /api/v1/audit"), client_support="admin read"),
        },
        "monthly_quota": monthly,
    }
