#!/usr/bin/env python3
"""Shared user-scoped client for the existing time/pro6000d broker API.

This module deliberately contains no Slurm, SQLite, sudo, UID override, or
administrator gateway code.  The broker obtains the caller's real UID from
the Unix socket peer credentials.
"""

from __future__ import print_function

import datetime as _datetime
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
        self._ensure_directory()

    def _ensure_directory(self):
        if self.directory.exists() or self.directory.is_symlink():
            directory_stat = os.lstat(str(self.directory))
            if not stat.S_ISDIR(directory_stat.st_mode) or stat.S_ISLNK(directory_stat.st_mode):
                raise TimeError("request_ledger_unsecure",
                                "local request state path is not a real directory")
        self.directory.mkdir(parents=True, exist_ok=True)
        _secure_mode(self.directory, 0o700)

    def _read(self):
        try:
            if self.path.is_symlink():
                raise TimeError("request_ledger_unsecure",
                                "local request ledger must not be a symlink")
            with self.path.open("r", encoding="utf-8") as stream:
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

    def _write(self, value):
        self._ensure_directory()
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
            _secure_mode(self.path, 0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def new_id(self):
        return "req-" + uuid.uuid4().hex

    def reserve(self, request_id, method, path, body):
        if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", request_id or ""):
            raise ValueError("request ID must contain 8-80 letters, digits, or hyphens")
        ledger = self._read()
        digest = request_hash(method, path, body)
        prior = ledger["requests"].get(request_id)
        if prior and prior.get("request_hash") != digest:
            raise TimeError("idempotency_conflict",
                            "request ID is already bound to different request body",
                            status=409)
        if not prior:
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
            self._write(ledger)
        return digest

    def update(self, request_id, **fields):
        ledger = self._read()
        record = ledger["requests"].get(request_id)
        if not record:
            return
        record.update(fields)
        record["updated_at"] = utc_now()
        self._write(ledger)

    def get(self, request_id):
        return self._read()["requests"].get(request_id)

    def all(self):
        return list(self._read()["requests"].values())


class BrokerClient(object):
    def __init__(self, socket_path=None):
        self.socket_path = socket_path or os.environ.get(
            "PRO6000D_TRAINING_SOCKET", DEFAULT_SOCKET)

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
                    raise ServiceUnavailable("backend response exceeds 8 MiB")
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
            raw = b"".join(chunks).split(b"\n", 1)[0]
            if not raw:
                raise ServiceUnavailable("backend returned no response")
            try:
                response = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ServiceUnavailable("backend returned invalid JSON",
                                         details=str(exc))
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
        if not isinstance(response, dict):
            raise ServiceUnavailable("backend response is not an object")
        if not response.get("ok"):
            error = response.get("error") or {}
            status = error.get("status")
            code = error.get("code") or "backend_error"
            message = error.get("message") or "backend rejected request"
            retryable = status in (408, 429, 502, 503, 504)
            raise TimeError(code, message, status=status,
                            details=error.get("details"),
                            retryable=retryable, outcome="rejected")
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
    if state == "SUBMITTING" or "uncertain" in reason or state == "COMPLETION_UNKNOWN":
        return "unknown"
    if state == "CANCELLATION_REQUESTED":
        return "unknown"
    return "confirmed"


def format_error(exc, request_id=None):
    return error_envelope(exc, request_id=request_id)


def feature_capabilities():
    """Capabilities known from observed legacy routes, without guessing server state."""
    return {
        "source": "client_compatibility_map",
        "server_capabilities_endpoint": "unknown",
        "preview_read_only": True,
        "note": "client compatibility information only; server capabilities have not been verified",
        "commands": {
            "identity": {"available": None, "route": "GET /api/v1/me"},
            "state": {"available": None, "route": "GET /api/v1/state"},
            "queue": {"available": None, "route": "GET /api/v1/jobs"},
            "validate": {"available": None, "route": "POST /api/v1/jobs/validate"},
            "submit": {"available": None, "route": "POST /api/v1/jobs"},
            "cancel": {"available": None, "route": "POST /api/v1/jobs/{id}/cancel"},
            "logs_tail": {"available": None, "route": "GET /api/v1/jobs/{id}/log", "max_bytes": 1048576},
            "logs_follow": {"available": False, "reason": "backend exposes bounded tail only"},
            "native_argv": {"available": False, "reason": "backend accepts legacy shell command"},
            "multi_node": {"available": False, "reason": "no authoritative multi-node contract"},
            "applications": {"available": None, "route": "GET/POST /api/v1/applications"},
            "admin_preview_apply": {"available": None, "route": "existing preview/apply routes"},
            "audit": {"available": None, "route": "GET /api/v1/audit", "admin_only": True},
        },
        "monthly_quota": {
            "state": "unknown",
            "enforcement_ready": None,
            "accounting": "unknown",
            "reason": "not verified by this client; see server state",
        },
    }
