"""Prepared offline validation; do not run without explicit test authorization.

TIME_VALIDATION_CLIENT_DIR must point to a separately staged candidate. Socket
operations are replaced with in-process fakes; no live endpoint is contacted.
"""
import importlib.util
import os
import pathlib
import socket
import struct
import unittest
from unittest import mock


def load_candidate():
    folder = os.environ.get("TIME_VALIDATION_CLIENT_DIR")
    if not folder:
        raise RuntimeError("TIME_VALIDATION_CLIENT_DIR must name an isolated candidate")
    target = pathlib.Path(folder).resolve(strict=True) / "time_core.py"
    if target.is_symlink() or not target.is_file():
        raise RuntimeError("candidate source unavailable")
    spec = importlib.util.spec_from_file_location("validation_time_core", str(target))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeBrokerSocket:
    def __init__(self, reply, uid=0):
        self.reply = [reply]
        self.uid = uid
        self.sent = []

    def settimeout(self, timeout):
        pass

    def connect(self, path):
        pass

    def getsockopt(self, level, option, size):
        return struct.pack("3i", 123, self.uid, 0)

    def sendall(self, value):
        self.sent.append(value)

    def recv(self, size):
        return self.reply.pop(0) if self.reply else b""

    def close(self):
        pass


@unittest.skipUnless(hasattr(socket, "SO_PEERCRED"), "requires Linux peer credential constant")
class BrokerProtocolValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = load_candidate()

    def request(self, fake):
        with mock.patch.object(self.core.socket, "socket", return_value=fake):
            return self.core.BrokerClient().request("POST", "/api/v1/jobs", {"name": "example"}, mutation=True)

    def test_untrusted_peer_is_rejected_before_send(self):
        fake = FakeBrokerSocket(b"", uid=1001)
        with self.assertRaises(self.core.ServiceUnavailable):
            self.request(fake)
        self.assertEqual(fake.sent, [])

    def test_truncated_frame_does_not_establish_success(self):
        fake = FakeBrokerSocket(b'{"ok":true,"data":{"job":{"id":"a","state":"PENDING"}}}')
        with self.assertRaises(self.core.UnknownOutcome):
            self.request(fake)
        self.assertEqual(len(fake.sent), 1)

    def test_malformed_rejection_status_is_unknown(self):
        fake = FakeBrokerSocket(b'{"ok":false,"error":{"code":"error","message":"error"}}\n')
        with self.assertRaises(self.core.UnknownOutcome):
            self.request(fake)

    def test_missing_or_mismatched_job_is_unknown(self):
        outcome = self.core.mutation_result_outcome
        self.assertEqual(outcome("POST", "/api/v1/jobs", {}), "unknown")
        self.assertEqual(outcome("POST", "/api/v1/jobs/one/cancel",
                                 {"job": {"id": "two", "state": "CANCELLED"}}), "unknown")

    def test_scheduler_completion_unknown_stays_unknown(self):
        job = {"id": "one", "state": "RUNNING", "reason_code": "completion_unknown"}
        self.assertEqual(self.core.mutation_result_outcome("POST", "/api/v1/jobs", {"job": job}), "unknown")


if __name__ == "__main__":
    unittest.main()
