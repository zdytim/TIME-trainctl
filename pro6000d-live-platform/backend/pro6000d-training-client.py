#!/usr/bin/env python3
"""Cockpit bridge: forward JSON lines to the root-owned Unix socket.

This executable is launched by cockpit.spawn() as the logged-in session user.
The server uses the resulting Unix peer credentials as the only identity.
"""

import json
import os
import socket
import sys

SOCKET = os.environ.get("PRO6000D_TRAINING_SOCKET", "/run/pro6000d-training/backend.sock")

def main() -> int:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(45)
    try:
        sock.connect(SOCKET)
    except OSError as exc:
        print(json.dumps({"ok": False, "error": {"status": 503, "code": "backend_unavailable", "message": str(exc)}}, ensure_ascii=False), flush=True)
        return 1
    with sock, sock.makefile("rwb") as peer:
        line = sys.stdin.buffer.readline(1048577)
        if not line.strip() or len(line) > 1048576:
            return 1
        if not line.endswith(b"\n"):
            line += b"\n"
        peer.write(line); peer.flush()
        response = peer.readline(8388609)
        if not response or len(response) > 8388608:
            return 1
        sys.stdout.buffer.write(response); sys.stdout.buffer.flush()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
