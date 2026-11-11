"""Host-provided, out-of-band consent channel for MCP mutations.

The host must own and protect the other end, present the complete request to a
human, and return a fresh action-bound decision. This module supplies no UI and
cannot establish that a same-UID peer really is such a trusted host by itself.
"""
import hashlib
import json
import os
import socket
import struct
import uuid


class ConsentUnavailable(Exception):
    pass


class HostConsent:
    def __init__(self, fd):
        if os.geteuid() == 0:
            raise ConsentUnavailable("root MCP process is not supported")
        self.sock = socket.socket(fileno=os.dup(fd))
        try:
            if self.sock.family != socket.AF_UNIX or self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
                raise ConsentUnavailable("consent channel must be an inherited Unix stream")
            _pid, uid, _gid = struct.unpack("3i", self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.geteuid():
                raise ConsentUnavailable("consent peer UID mismatch")
            self.sock.settimeout(90)
        except Exception:
            self.sock.close()
            raise

    def authorize(self, method, path, body):
        action = {"method": method.upper(), "path": path, "body": body}
        canonical = json.dumps(action, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        request_id = uuid.uuid4().hex
        request = json.dumps({"version": 1, "request_id": request_id, "sha256": digest,
                              "action": action}, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(request) > 65536:
            raise ConsentUnavailable("action too large for consent channel")
        try:
            self.sock.sendall(request)
            data = bytearray()
            while len(data) <= 4096 and b"\n" not in data:
                block = self.sock.recv(4097 - len(data))
                if not block:
                    break
                data.extend(block)
            if len(data) > 4096 or not data.endswith(b"\n") or data.count(b"\n") != 1:
                raise ConsentUnavailable("incomplete consent decision")
            decision = json.loads(data.decode("utf-8"))
            if not isinstance(decision, dict) or decision.get("version") != 1 or decision.get("request_id") != request_id or decision.get("sha256") != digest:
                raise ConsentUnavailable("consent decision does not match this action")
            return decision.get("approved") is True
        except (OSError, UnicodeError, ValueError, ConsentUnavailable) as exc:
            self.sock.close()
            raise ConsentUnavailable("consent channel unavailable or invalid") from exc


def inherited_host_consent():
    raw = os.environ.get("TIME_MCP_CONSENT_FD")
    if not raw or not raw.isascii() or not raw.isdigit():
        return None
    try:
        return HostConsent(int(raw))
    except (OSError, ValueError, TypeError, ConsentUnavailable):
        return None
