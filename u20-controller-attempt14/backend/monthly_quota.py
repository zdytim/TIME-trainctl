"""Projection and command client for the controller-owned monthly ledger.

The API never answers an allocation request from this module. Slurmctld makes
the admission decision while holding its own allocation synchronization; this
client is limited to durable Grant and read-only Snapshot operations.
"""

from __future__ import annotations

import datetime as dt
import re
import socket
from typing import Any, Dict, Optional

UTC = dt.timezone.utc
BEIJING = dt.timezone(dt.timedelta(hours=8))
LIMITS = (("debug", 5 * 3600), ("important", 100 * 3600), ("normal", 500 * 3600))
CONTROLLER_UNAVAILABLE = "月度控制器不可用；新 GPU 分配由控制器安全拒绝，已有任务继续运行"


def month_now() -> str:
    return dt.datetime.now(BEIJING).strftime("%Y-%m")


class ControllerError(RuntimeError):
    pass


class ControllerClient:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path

    def _call(self, line: str) -> str:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        try:
            sock.connect(self.socket_path)
            sock.sendall((line + "\n").encode("ascii"))
            data = bytearray()
            while len(data) < 4096:
                block = sock.recv(4096)
                if not block:
                    break
                data.extend(block)
                if b"\n" in data:
                    break
            response = bytes(data).split(b"\n", 1)[0].decode("ascii")
        except OSError as exc:
            raise ControllerError(str(exc)) from exc
        finally:
            sock.close()
        if response.startswith("ERR "):
            raise ControllerError(response[4:])
        if not response.startswith("OK "):
            raise ControllerError("controller response invalid")
        return response[3:]

    def snapshot(self, uid: int) -> Dict[str, Any]:
        fields = self._call(f"SNAPSHOT {int(uid)}").split()
        if len(fields) != 8:
            raise ControllerError("controller snapshot fields invalid")
        revision, response_uid, month, used_ns, credit_ns, remaining_ns, tier, health = fields
        if int(response_uid) != uid or not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ControllerError("controller snapshot identity invalid")
        return {"revision": int(revision), "uid": uid, "month": month,
                "used_gpu_seconds": int(used_ns) // 1_000_000_000,
                "additional_gpu_seconds": int(credit_ns) // 1_000_000_000,
                "remaining_gpu_seconds": int(remaining_ns) // 1_000_000_000,
                "tier_number": int(tier), "health": health}

    def grant(self, application_id: str, uid: int, month: str,
              gpu_seconds: int, actor_uid: int) -> Dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,160}", application_id):
            raise ControllerError("application identity invalid")
        if not re.fullmatch(r"\d{4}-\d{2}", month) or gpu_seconds <= 0:
            raise ControllerError("grant values invalid")
        return {"revision": int(self._call(
            f"GRANT {application_id} {int(uid)} {month} {int(gpu_seconds)} {int(actor_uid)} api"))}


class MonthlyLedger:
    def __init__(self, service):
        self.service = service
        config = service.config.get("monthly_quota", {})
        self.enabled = bool(config.get("enabled", False))
        self.socket_path = config.get("controller_socket", "/var/lib/slurm/monthly-quota/controller.sock")
        self.controller = ControllerClient(self.socket_path)
        self.status = "disabled" if not self.enabled else "unknown"
        self.error: Optional[str] = None
        self.observed_at: Optional[str] = None
        # Keep the request table for UI/application idempotency. The controller
        # journal is authoritative for usage and additional credit.
        service.store.db.executescript("""
          CREATE TABLE IF NOT EXISTS monthly_requests(
            uid INTEGER NOT NULL,request_id TEXT NOT NULL,application_id TEXT NOT NULL,
            PRIMARY KEY(uid,request_id));
        """)

    def summary(self, uid: int) -> Dict[str, Any]:
        month = month_now()
        if not self.enabled:
            return self._empty(month, "disabled", None)
        try:
            snapshot = self.controller.snapshot(uid)
            self.status, self.error, self.observed_at = "ok", None, dt.datetime.now(UTC).isoformat()
            used = snapshot["used_gpu_seconds"]
            extra = snapshot["additional_gpu_seconds"]
            left = used
            tiers = []
            for name, base in LIMITS:
                limit = base + (extra if name == "normal" else 0)
                consumed = min(left, limit)
                tiers.append({"name": name, "limit_gpu_seconds": limit,
                              "used_gpu_seconds": consumed,
                              "remaining_gpu_seconds": limit - consumed})
                left = max(0, left - limit)
            current = {3: "debug", 2: "important", 1: "normal"}.get(snapshot["tier_number"])
            return {"enabled": True, "mode": "enforcing", "enforcement_ready": snapshot["health"] == "ok",
                    "enforcement_limitation": "月度 GPU 小时由 Slurmctld 控制器在实际资源分配前串行裁决。",
                    "accounting_status": snapshot["health"], "accounting_error": None,
                    "observed_at": self.observed_at, "enabled_at": None, "timezone": "Asia/Shanghai",
                    "month": snapshot["month"], "used_gpu_seconds": used,
                    "remaining_gpu_seconds": snapshot["remaining_gpu_seconds"],
                    "overage_gpu_seconds": 0, "current_tier": current,
                    "additional_gpu_seconds": extra, "added_normal_gpu_seconds": extra,
                    "tiers": tiers, "usage_verified": snapshot["health"] == "ok",
                    "controller_revision": snapshot["revision"]}
        except (ControllerError, ValueError) as exc:
            self.status, self.error = "unknown", str(exc)[:400]
            return self._empty(month, "unknown", self.error)

    @staticmethod
    def _empty(month: str, status: str, error: Optional[str]) -> Dict[str, Any]:
        return {"enabled": status != "disabled", "mode": "enforcing" if status != "disabled" else "disabled",
                "enforcement_ready": False, "enforcement_limitation": CONTROLLER_UNAVAILABLE,
                "accounting_status": status, "accounting_error": error, "observed_at": None,
                "enabled_at": None, "timezone": "Asia/Shanghai", "month": month,
                "used_gpu_seconds": None, "remaining_gpu_seconds": None,
                "overage_gpu_seconds": None, "current_tier": None,
                "additional_gpu_seconds": None, "added_normal_gpu_seconds": None,
                "tiers": [{"name": n, "limit_gpu_seconds": s,
                           "used_gpu_seconds": None, "remaining_gpu_seconds": None}
                          for n, s in LIMITS], "usage_verified": False}

    def refresh(self, force: bool = False) -> None:
        # Snapshots are read when the API projection is requested. There is no
        # periodic cached ALLOW result and no sacct decision path.
        return None

    def grant(self, application_id: str, uid: int, month: str,
              gpu_seconds: int, actor_uid: int) -> Dict[str, Any]:
        if not self.enabled:
            raise ControllerError("monthly controller disabled")
        return self.controller.grant(application_id, uid, month, gpu_seconds, actor_uid)

    def block_reason(self, uid: int):
        # The API queues/submits the job. Slurmctld is the only allocation
        # authority and re-evaluates after waiting for its controller lock.
        return None

    def job_usage(self, row):
        # No stale local estimate is returned until a read-only per-job
        # snapshot endpoint is added to the controller protocol.
        return None
