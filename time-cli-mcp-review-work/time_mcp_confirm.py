#!/usr/bin/env python3
"""Launch time-mcp with a terminal-only, action-bound human consent host."""
import hashlib
import json
import os
import pathlib
import re
import select
import socket
import subprocess
import sys
import threading
import time


def reply(sock, request, approved):
    decision = {"version": 1, "request_id": request["request_id"],
                "sha256": request["sha256"], "approved": bool(approved)}
    sock.sendall(json.dumps(decision, separators=(",", ":")).encode("ascii") + b"\n")


def validate(raw):
    req = json.loads(raw.decode("utf-8"))
    if not isinstance(req, dict) or req.get("version") != 1:
        raise ValueError("invalid consent request")
    nonce, digest, action = req.get("request_id"), req.get("sha256"), req.get("action")
    if (not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce) or
            not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or
            not isinstance(action, dict) or set(action) != {"method", "path", "body"} or
            action["method"] != "POST" or not isinstance(action["body"], dict) or
            not isinstance(action["path"], str)):
        raise ValueError("invalid consent fields")
    path = action["path"]
    if path != "/api/v1/jobs" and not re.fullmatch(r"/api/v1/jobs/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}/cancel", path):
        raise ValueError("unsupported action")
    canonical = json.dumps(action, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != digest:
        raise ValueError("action digest mismatch")
    return req


def ask(tty, request):
    # ASCII JSON escapes terminal control characters and visual direction marks.
    display = json.dumps(request["action"], ensure_ascii=True, sort_keys=True, indent=2)
    message = ("\nTIME MCP requests permission for this exact action:\n" + display +
               "\nType YES to approve within 60 seconds; anything else rejects: ")
    os.write(tty, message.encode("ascii"))
    deadline = time.monotonic() + 60
    answer = bytearray()
    while len(answer) < 32:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([tty], [], [], remaining)[0]:
            break
        byte = os.read(tty, 1)
        if not byte or byte in (b"\r", b"\n"):
            break
        answer.extend(byte)
    os.write(tty, b"\n")
    return bytes(answer) == b"YES"


def serve(sock, tty):
    pending = bytearray()
    try:
        while True:
            block = sock.recv(4096)
            if not block:
                return
            pending.extend(block)
            if len(pending) > 65536:
                return
            while b"\n" in pending:
                line, _, remaining = pending.partition(b"\n")
                pending = bytearray(remaining)
                request = validate(line)
                reply(sock, request, ask(tty, request))
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        # Closing the channel makes the child fail closed.
        pass
    finally:
        sock.close()


def main():
    script = pathlib.Path(__file__).resolve().parent / "time_mcp.py"
    env = os.environ.copy()
    env.pop("TIME_MCP_CONSENT_FD", None)
    try:
        tty = os.open("/dev/tty", os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
        if not os.isatty(tty):
            raise OSError("not an interactive terminal")
    except OSError:
        print("time-mcp-confirm: no interactive terminal; starting read-only MCP", file=sys.stderr)
        os.execve(sys.executable, [sys.executable, str(script)], env)
    host, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    env["TIME_MCP_CONSENT_FD"] = str(child.fileno())
    try:
        process = subprocess.Popen([sys.executable, str(script)], env=env,
                                   pass_fds=(child.fileno(),))
        child.close()
        worker = threading.Thread(target=serve, args=(host, tty), daemon=True)
        worker.start()
        return process.wait()
    finally:
        child.close()
        host.close()
        os.close(tty)


if __name__ == "__main__":
    sys.exit(main())
