"""Offline protocol validation using fakes only; requires explicit test approval.

Set TIME_VALIDATION_BROKER_DIR and TIME_VALIDATION_CLIENT_DIR to isolated
candidate directories. No live sockets, real terminal, database or GPU is used.
"""
import hashlib
import importlib.util
import json
import os
import pathlib
import unittest


def load(folder_env, filename, name):
    value = os.environ.get(folder_env)
    if not value:
        raise RuntimeError(folder_env + " must name an isolated candidate directory")
    target = pathlib.Path(value).resolve(strict=True) / filename
    if not target.is_file() or target.is_symlink():
        raise RuntimeError("candidate source unavailable")
    spec = importlib.util.spec_from_file_location(name, str(target))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSocket:
    def __init__(self, replies=(), send_failure=False):
        self.replies = list(replies)
        self.send_failure = send_failure
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def sendall(self, data):
        self.sent.append(data)
        if self.send_failure:
            raise OSError("injected partial send")

    def recv(self, limit):
        return self.replies.pop(0) if self.replies else b""


class ControllerClientValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load("TIME_VALIDATION_BROKER_DIR", "controller_bridge.py", "validation_controller_bridge")

    def bridge(self, fake):
        bridge = self.module.ControllerBridge("/not-used", 1234)
        bridge._connect = lambda: fake
        return bridge

    def test_snapshot_parses_exact_controller_fields(self):
        fake = FakeSocket([b"OK 202610 1 18000 0 3600 0 2181600 0 2163600 0 0 0 4 1 1791000000 0 42\n"])
        result = self.bridge(fake).snapshot(1001)
        self.assertEqual(result["tier"], "important")
        self.assertEqual(result["used"], {"gpu_seconds": 18000, "nanoseconds": 0})
        self.assertEqual(result["active_gpu_count"], 4)
        self.assertEqual(result["wal_sequence"], 42)
        self.assertEqual(fake.sent, [b"SNAP 1001\n"])

    def test_incomplete_snapshot_is_unknown_not_zero(self):
        with self.assertRaises(self.module.ControllerUnavailable):
            self.bridge(FakeSocket([b"OK 202610 1"])).snapshot(1001)

    def test_grant_partial_send_is_unknown_and_keeps_identity(self):
        fake = FakeSocket(send_failure=True)
        with self.assertRaises(self.module.ControllerOutcomeUnknown):
            self.bridge(fake).grant(1001, 202610, 36000, "app-123", "uid-1000")
        self.assertEqual(fake.sent, [b"GRANT 1001 202610 36000 app-123 uid-1000\n"])

    def test_bad_grant_fields_never_send(self):
        fake = FakeSocket([b"OK\n"])
        with self.assertRaises(ValueError):
            self.bridge(fake).grant(1001, 202610, 36000, "bad id", "uid-1000")
        self.assertEqual(fake.sent, [])

    def test_connection_failure_before_send_is_unavailable(self):
        bridge = self.module.ControllerBridge("/not-used", 1234)
        def unavailable():
            raise self.module.ControllerUnavailable("injected unavailable")
        bridge._connect = unavailable
        with self.assertRaises(self.module.ControllerUnavailable) as error:
            bridge.grant(1001, 202610, 36000, "app-123", "uid-1000")
        self.assertNotIsInstance(error.exception, self.module.ControllerOutcomeUnknown)


class TerminalHostValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load("TIME_VALIDATION_CLIENT_DIR", "time_mcp_confirm.py", "validation_terminal_host")

    def request(self):
        action = {"method": "POST", "path": "/api/v1/jobs", "body": {"command": "python train.py"}}
        canonical = json.dumps(action, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return {"version": 1, "request_id": "a" * 32,
                "sha256": hashlib.sha256(canonical).hexdigest(), "action": action}

    def test_action_digest_binds_body(self):
        request = self.request()
        self.assertEqual(self.module.validate(json.dumps(request).encode("utf-8")), request)
        request["action"]["body"]["command"] = "changed"
        with self.assertRaises(ValueError):
            self.module.validate(json.dumps(request).encode("utf-8"))

    def test_unsupported_host_action_is_rejected(self):
        request = self.request()
        request["action"]["path"] = "/api/v1/unknown"
        with self.assertRaises(ValueError):
            self.module.validate(json.dumps(request).encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
