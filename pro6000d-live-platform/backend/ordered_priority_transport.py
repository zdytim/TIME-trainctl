"""Unadvertised isolated ordered transport; not a production activation path."""
import hashlib
import json
import re

from controller_bridge import ControllerBridge, ControllerOutcomeUnknown, ControllerUnavailable

_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_REQUEST = re.compile(r"[A-Za-z0-9-]{8,80}\Z")
_FIELDS = {"action", "actor_uid", "actor_name", "uid", "username", "enabled",
           "expected_enabled", "expected_version", "identity_generation"}


class OrderedPriorityTransport:
    def __init__(self, path, controller_uid, expected_pid, expected_epoch, timeout=2.0, *, release_manifest=None, open_snapshot=False):
        # These identities must come from a separately trusted local supervisor,
        # never from the request payload or an HTTP client.
        if (type(controller_uid) is not int or controller_uid < 0 or
                type(expected_pid) is not int or expected_pid <= 0 or
                not isinstance(expected_epoch, str) or not _HEX32.fullmatch(expected_epoch) or
                type(open_snapshot) is not bool or (open_snapshot and not release_manifest)):
            raise ValueError("trusted controller identity required")
        self.bridge = ControllerBridge(path, controller_uid, timeout, release_manifest=release_manifest)
        self.bridge.expected_pid = expected_pid
        self.expected_pid = expected_pid
        self.release_manifest = release_manifest
        self.open_snapshot = open_snapshot
        self.epoch = expected_epoch

    def _exchange(self, message, mutation=False):
        # Legacy isolated tests may omit a release manifest. A production
        # priority mutation also requires fence_top(), which refuses that mode.
        if self.release_manifest is None:
            return self.bridge._exchange(message, mutation=mutation)
        if self.bridge.release_manifest != self.release_manifest or self.bridge.expected_pid != self.expected_pid:
            raise ControllerUnavailable('controller process identity changed')
        return self.bridge._exchange(message, mutation=mutation, required_pid=self.expected_pid)

    def fence_top(self):
        """Engage the admission fence through the attested controller."""
        if self.release_manifest is None:
            raise ControllerUnavailable('controller release attestation required')
        if self._exchange('TOP_FENCE1 0 1', mutation=True) != 'OK':
            raise ControllerOutcomeUnknown('unverified priority fence reply')

    def status(self):
        command = 'ORDERED_STATUS3 0 ' if self.open_snapshot else 'ORDERED_STATUS2 0 '
        fields = self._exchange(command + self.epoch).split(' ')
        allowed = ('OPEN', 'CLOSED')
        if (len(fields) != (7 if self.open_snapshot else 6) or fields[0] != 'OK' or fields[1] != self.epoch or
                not _HEX32.fullmatch(fields[2]) or not re.fullmatch(r'0|[1-9][0-9]*', fields[3]) or
                int(fields[3]) > 2**64 - 1 or fields[-2] not in allowed or fields[-1] != 'RECOVERED' or
                (self.open_snapshot and (not re.fullmatch(r'0|[1-9][0-9]*', fields[4]) or int(fields[4]) > 2**64 - 1))):
            raise ControllerUnavailable('unverified controller status')
        result = {'epoch': self.epoch, 'boot': fields[2], 'watermark': int(fields[3])}
        if self.open_snapshot:
            result['ledger_seq'] = int(fields[4])
            result['fenced'] = fields[5] == 'CLOSED'
        return result

    def chain_lookup(self, status, sequence):
        """Read one historical chain hash from the authenticated same boot.

        The controller query checks installation and boot under its ledger lock.
        This does not replace the separate protected broker anchor.
        """
        if (not isinstance(status, dict) or status.get('epoch') != self.epoch or
                not isinstance(status.get('boot'), str) or not _HEX32.fullmatch(status['boot']) or
                type(status.get('watermark')) is not int or type(sequence) is not int or
                not 0 <= sequence <= status['watermark'] <= 2**64 - 1):
            raise ValueError('invalid controller snapshot or sequence')
        if self.open_snapshot:
            if (type(status.get('fenced')) is not bool or type(status.get('ledger_seq')) is not int or
                    not 0 <= status['ledger_seq'] <= 2**64 - 1):
                raise ValueError('missing controller snapshot state')
            fence = int(status['fenced'])
            frame = 'ORDERED_CHAIN2 0 {} {} {} {} {} {}'.format(self.epoch, status['boot'], status['watermark'], status['ledger_seq'], fence, sequence)
            fields = self._exchange(frame).split(' ')
            if (len(fields) != 6 or fields[0] != 'OK' or fields[1] != str(sequence) or
                    not _HEX64.fullmatch(fields[2]) or fields[3:] != [str(status['watermark']), str(status['ledger_seq']), str(fence)]):
                raise ControllerUnavailable('unverified controller chain response')
        else:
            fields = self._exchange('ORDERED_CHAIN1 0 {} {} {}'.format(self.epoch, status['boot'], sequence)).split(' ')
            if len(fields) != 3 or fields[0] != 'OK' or fields[1] != str(sequence) or not _HEX64.fullmatch(fields[2]):
                raise ControllerUnavailable('unverified controller chain response')
        return fields[2]

    def user_status(self, status, uid, username, identity_generation):
        """Read a policy at one authenticated boot and watermark.

        The caller must separately authenticate the controller history prefix
        against the protected anchor before presenting or relying on this data.
        """
        if (not isinstance(status, dict) or status.get('epoch') != self.epoch or
                not isinstance(status.get('boot'), str) or not _HEX32.fullmatch(status['boot']) or
                type(status.get('watermark')) is not int or not 0 <= status['watermark'] <= 2**64 - 1 or
                type(uid) is not int or not 0 <= uid <= 2**32 - 1 or
                not isinstance(username, str) or not _NAME.fullmatch(username) or
                not isinstance(identity_generation, str) or not _HEX32.fullmatch(identity_generation)):
            raise ValueError('invalid controller snapshot or account identity')
        if self.open_snapshot:
            if (type(status.get('fenced')) is not bool or type(status.get('ledger_seq')) is not int or
                    not 0 <= status['ledger_seq'] <= 2**64 - 1):
                raise ValueError('missing controller snapshot state')
            fence = int(status['fenced'])
            frame = 'ORDERED_USER2 0 {} {} {} {} {} {} {} {}'.format(
                self.epoch, status['boot'], status['watermark'], status['ledger_seq'], fence, uid, username, identity_generation)
        else:
            frame = 'ORDERED_USER1 0 {} {} {} {} {}'.format(self.epoch, status['boot'], uid, username, identity_generation)
        fields = self._exchange(frame).split(' ')
        expected = ['OK', self.epoch, status['boot'], str(uid), username, identity_generation]
        suffix = [str(status['watermark']), str(status['ledger_seq']), str(fence)] if self.open_snapshot else [str(status['watermark'])]
        if (len(fields) != (11 if self.open_snapshot else 9) or fields[:6] != expected or fields[6] not in ('0', '1') or
                not re.fullmatch(r'0|[1-9][0-9]*', fields[7]) or int(fields[7]) > 2**53 - 1 or
                fields[8:] != suffix):
            raise ControllerUnavailable('unverified controller user status')
        return {'uid': uid, 'username': username, 'identity_generation': identity_generation,
                'enabled': fields[6] == '1', 'version': int(fields[7])}

    def apply_baseline(self, sequence, request_id, uid, username, generation):
        """Submit an ordered baseline; caller must durably journal intent first."""
        if (type(sequence) is not int or not 0 < sequence < 2**63 or
                not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id) or
                type(uid) is not int or not 0 <= uid <= 2**32-1 or
                not isinstance(username, str) or not _NAME.fullmatch(username) or
                not isinstance(generation, str) or not _HEX32.fullmatch(generation)):
            raise ValueError('invalid baseline identity')
        frame = 'ORDERED_BASE1 0 %s %d %s %d %s %s' % (self.epoch, sequence, request_id, uid, username, generation)
        expected = 'OK %s %d %s %d %s %s' % (self.epoch, sequence, request_id, uid, username, generation)
        if self._exchange(frame, mutation=True) != expected:
            raise ControllerOutcomeUnknown('baseline outcome unknown')

    def apply(self, sequence, request_id, digest, payload, *, expected_status=None):
        if (type(sequence) is not int or not 1 <= sequence <= 2**64 - 1 or
                not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id) or
                not isinstance(digest, str) or not _HEX64.fullmatch(digest) or
                not isinstance(payload, dict) or set(payload) != _FIELDS or
                payload["action"] != "set_top_priority"):
            raise ValueError("invalid ordered request")
        for key in ("actor_uid", "uid"):
            if type(payload[key]) is not int or not 0 <= payload[key] <= 2**32 - 1:
                raise ValueError("invalid uid")
        for key in ("actor_name", "username"):
            if not isinstance(payload[key], str) or not _NAME.fullmatch(payload[key]):
                raise ValueError("unsupported name")
        if (type(payload["enabled"]) is not bool or type(payload["expected_enabled"]) is not bool or
                payload["enabled"] == payload["expected_enabled"] or
                type(payload["expected_version"]) is not int or
                not 0 <= payload["expected_version"] < 2**53 - 1 or
                not isinstance(payload["identity_generation"], str) or
                not _HEX32.fullmatch(payload["identity_generation"])):
            raise ValueError("invalid policy transition")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False).encode("utf-8")
        if hashlib.sha256(canonical).hexdigest() != digest:
            raise ValueError("digest mismatch")
        status = self.status()
        if expected_status is not None and status != expected_status:
            raise ControllerUnavailable("controller status changed before apply")
        if self.open_snapshot and status.get('fenced') is not True:
            raise ControllerUnavailable('policy mutation requires a closed fence')
        watermark = status["watermark"]
        if sequence > watermark and (watermark == 2**64 - 1 or sequence != watermark + 1):
            raise ControllerUnavailable("ordered sequence gap")
        frame = "ORDERED_APPLY2 {} {} {} {} {} {} {} {} {} {} {} {} {}".format(
            self.epoch, status["boot"], sequence, request_id, digest, payload["actor_uid"],
            payload["actor_name"], payload["uid"], payload["username"], int(payload["enabled"]),
            int(payload["expected_enabled"]), payload["expected_version"], payload["identity_generation"])
        if len(frame.encode("ascii")) + 1 > 1024:
            raise ValueError("frame too long")
        reply = self._exchange(frame, mutation=True).split(" ")
        expected = ["OK", str(payload["uid"]), str(int(payload["enabled"])),
                    str(payload["expected_version"] + 1), payload["identity_generation"],
                    self.epoch, str(sequence), request_id, digest]
        if reply != expected:
            raise ControllerOutcomeUnknown("unverified ordered mutation result")
        return {"uid": payload["uid"], "enabled": payload["enabled"],
                "version": payload["expected_version"] + 1, "epoch": self.epoch,
                "identity_generation": payload["identity_generation"]}
