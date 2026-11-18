#!/usr/bin/env python3
"""Root-owned, UID-authenticated API for the pro6000d Cockpit plugin.

The process is intentionally small and uses only the Python standard library.
It accepts newline-delimited JSON over a mode 0660 Unix socket.  The peer UID
is obtained from SO_PEERCRED for every connection; no username or role from
the browser is trusted.
"""

from __future__ import annotations

import argparse
from monthly_quota import MonthlyLedger, month_now
from monthly_reservation import ReservationCoordinator, ReservationDeferred, ReservationUncertain
from controller_bridge import ControllerBridge, PinnedControllerBridge, ActualUsageControllerBridge, ControllerUnavailable, ControllerOutcomeUnknown, PriorityBarrierWait
from controller_view import selectable_snapshot_view, unavailable
from activation_attestation import verify_release
import base64
import datetime as dt
import grp
import hashlib
import hmac
import json
import os
import pathlib
import pwd
import re
import shutil
import selectors
import signal
import socket
import struct
import stat
from contextlib import contextmanager, nullcontext
import sqlite3
import subprocess
import sys
import tempfile
import time
import threading
import traceback
import uuid
import xml.etree.ElementTree as ET
from collections import deque
from typing import Any, Dict, Iterable, List, Optional, Tuple


VERSION = "0.1.0"
DEFAULT_CONFIG = "/etc/pro6000d-training/config.json"
DEFAULT_SOCKET = "/run/pro6000d-training/backend.sock"
ACTIVE_STATES = ("WAITING_QUOTA", "CANCELLING_RESERVATION", "SUBMITTING", "PENDING", "RUNNING", "COMPLETING", "SUSPENDED")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SAFE_ENV = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def approval_expired(value: Optional[str], now: dt.datetime) -> Optional[bool]:
    """Return None for an unparseable or timezone-free approval expiry."""
    try:
        expiry = parse_iso(value)
        if expiry is None or expiry.tzinfo is None or expiry.utcoffset() is None:
            return None
        return expiry <= now
    except (TypeError, ValueError, OverflowError):
        return None


def json_object(value: Any, fallback: Any = None) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def run(argv: List[str], timeout: int = 8, uid: Optional[int] = None, strip: bool = True) -> Tuple[int, str, str]:
    """Run one fixed argv vector.  No shell is ever used."""
    if not argv or any("\x00" in str(x) for x in argv):
        raise ValueError("invalid argv")

    def drop_privileges() -> None:
        if uid is None or os.geteuid() != 0:
            return
        user = pwd.getpwuid(uid)
        os.setgroups(os.getgrouplist(user.pw_name, user.pw_gid))
        os.setgid(user.pw_gid)
        os.setuid(uid)

    try:
        env={"PATH":"/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin", "LANG":"C.UTF-8"}
        if os.environ.get("SLURM_CONF"):
            env["SLURM_CONF"] = os.environ["SLURM_CONF"]
        if uid is not None:
            account=pwd.getpwuid(uid)
            env.update(HOME=account.pw_dir,USER=account.pw_name,LOGNAME=account.pw_name)
        p = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           check=False, preexec_fn=drop_privileges if uid is not None else None)
        return p.returncode, p.stdout.strip() if strip else p.stdout, p.stderr.strip()
    except FileNotFoundError:
        return 127, "", "%s is not installed" % argv[0]
    except subprocess.TimeoutExpired:
        return 124, "", "command timed out"


def run_spawn_locked(argv: List[str], timeout: int = 20, attempted=None) -> Tuple[int, str, str]:
    """Launch without Python fork hooks or pre-exec callbacks under the coordinator.

    All broker-owned descriptors, including the coordinator lock, are CLOEXEC.
    Once spawn is attempted the caller must treat every failure as ambiguous.
    """
    if not argv or any("\x00" in str(x) for x in argv):
        raise ValueError("invalid argv")
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin", "LANG": "C.UTF-8"}
    if os.environ.get("SLURM_CONF"):
        env["SLURM_CONF"] = os.environ["SLURM_CONF"]
    fds = []
    pid = None
    selector = selectors.DefaultSelector()
    try:
        out_r, out_w = os.pipe2(os.O_CLOEXEC)
        fds.extend((out_r, out_w))
        err_r, err_w = os.pipe2(os.O_CLOEXEC)
        fds.extend((err_r, err_w))
        actions = [(os.POSIX_SPAWN_OPEN, 0, '/dev/null', os.O_RDONLY, 0),
                   (os.POSIX_SPAWN_DUP2, out_w, 1), (os.POSIX_SPAWN_DUP2, err_w, 2),
                   (os.POSIX_SPAWN_CLOSE, out_r), (os.POSIX_SPAWN_CLOSE, err_r),
                   (os.POSIX_SPAWN_CLOSE, out_w), (os.POSIX_SPAWN_CLOSE, err_w)]
        # Only setup failures above prove that no spawn was attempted.
        # Any exception at the call boundary may be asynchronous, including
        # OSError from a signal handler, so retain the uncertain claim.
        if attempted is not None:
            attempted[0] = True
        pid = os.posix_spawnp(argv[0], argv, env, file_actions=actions)
        for fd in (out_w, err_w):
            os.close(fd)
            fds.remove(fd)
        buffers = {out_r: bytearray(), err_r: bytearray()}
        for fd in buffers:
            os.set_blocking(fd, False)
            selector.register(fd, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('scheduler submission timed out')
            for key, _ in selector.select(remaining):
                data = os.read(key.fd, 65536)
                if not data:
                    selector.unregister(key.fd)
                elif len(buffers[key.fd]) + len(data) > 1048576:
                    raise RuntimeError('scheduler submission output too large')
                else:
                    buffers[key.fd].extend(data)
        while True:
            result, status = os.waitpid(pid, os.WNOHANG)
            if result:
                pid = None
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('scheduler submission timed out')
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        if os.WIFEXITED(status):
            returncode = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            returncode = -os.WTERMSIG(status)
        else:
            raise RuntimeError('unknown scheduler process status')
        return (returncode,
                buffers[out_r].decode('utf-8', 'replace').strip(),
                buffers[err_r].decode('utf-8', 'replace').strip())
    finally:
        selector.close()
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                reaped, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                reaped = pid
            if not reaped:
                # Do not wait for an uninterruptible child while holding the
                # coordinator. Only this daemon owns the outstanding wait.
                def reap_child(child):
                    try:
                        os.waitpid(child, 0)
                    except ChildProcessError:
                        pass
                try:
                    threading.Thread(target=reap_child, args=(pid,), daemon=True).start()
                except RuntimeError:
                    # Outcome is already unknown; the process can be reaped
                    # by normal service shutdown if a reaper cannot start.
                    pass
        for fd in fds:
            os.close(fd)


class Store:
    def __init__(self, path: str):
        self.path = path
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        # Durable request identities must survive a power loss before the
        # broker contacts the controller or reports an accepted operation.
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    def init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quota_overrides (
              uid INTEGER PRIMARY KEY, username TEXT NOT NULL, gpu_limit INTEGER NOT NULL,
              concurrent_gpu_limit INTEGER NOT NULL, active_job_limit INTEGER NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, scheduler_id TEXT, owner_uid INTEGER NOT NULL,
              owner_name TEXT NOT NULL, name TEXT NOT NULL, state TEXT NOT NULL,
              requested_gpu INTEGER NOT NULL, requested_cpu INTEGER NOT NULL,
              requested_mem_mb INTEGER NOT NULL, requested_minutes INTEGER NOT NULL,
              qos TEXT NOT NULL, workdir TEXT NOT NULL, env_name TEXT NOT NULL,
              command TEXT NOT NULL, output_path TEXT NOT NULL, reason_code TEXT,
              reason_text TEXT, temporary_application_id TEXT, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS jobs_owner_state ON jobs(owner_uid, state);
            CREATE TABLE IF NOT EXISTS job_dispatch_order (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL UNIQUE
            );
            INSERT OR IGNORE INTO job_dispatch_order(job_id)
              SELECT id FROM jobs WHERE NOT EXISTS
                (SELECT 1 FROM job_dispatch_order d WHERE d.job_id=jobs.id)
              ORDER BY created_at,id;
            CREATE TRIGGER IF NOT EXISTS jobs_dispatch_order_insert
              AFTER INSERT ON jobs BEGIN
                INSERT OR IGNORE INTO job_dispatch_order(job_id) VALUES (NEW.id);
              END;
            CREATE TABLE IF NOT EXISTS applications (
              id TEXT PRIMARY KEY, owner_uid INTEGER NOT NULL, owner_name TEXT NOT NULL,
              kind TEXT NOT NULL, status TEXT NOT NULL, requested_json TEXT NOT NULL,
              approved_json TEXT, reason TEXT NOT NULL, decision_reason TEXT,
              expires_at TEXT, bound_job_id TEXT, authorization_used INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
              id TEXT PRIMARY KEY, recipient_uid INTEGER NOT NULL, kind TEXT NOT NULL,
              title TEXT NOT NULL, body TEXT NOT NULL, read_at TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT, actor_uid INTEGER NOT NULL,
              actor_name TEXT NOT NULL, action TEXT NOT NULL, object_type TEXT NOT NULL,
              object_id TEXT NOT NULL, detail_json TEXT NOT NULL, result TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rule_previews (
              id TEXT PRIMARY KEY, actor_uid INTEGER NOT NULL, snapshot_json TEXT NOT NULL,
              proposed_json TEXT NOT NULL, impacts_json TEXT NOT NULL, created_at TEXT NOT NULL,
              used_at TEXT
            );
            CREATE TABLE IF NOT EXISTS quota_previews (
              id TEXT PRIMARY KEY, actor_uid INTEGER NOT NULL, target_uid INTEGER,
              target_scope TEXT NOT NULL, snapshot_json TEXT NOT NULL,
              proposed_json TEXT NOT NULL, impacts_json TEXT NOT NULL,
              created_at TEXT NOT NULL, used_at TEXT
            );
            """
        )

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json_object(row[0], default) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.db.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                        (key, json.dumps(value, ensure_ascii=False), utc_now()))

    def audit(self, uid: int, name: str, action: str, typ: str, oid: str, detail: Any, result: str) -> None:
        self.db.execute("INSERT INTO audit_log(actor_uid,actor_name,action,object_type,object_id,detail_json,result,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)", (uid, name, action, typ, oid,
                        json.dumps(detail, ensure_ascii=False), result, utc_now()))


class Service:
    def __init__(self, config_path: str):
        with open(config_path, encoding="utf-8") as fh:
            self.config = json.load(fh)
        # Enabling a partial priority runtime would silently bypass its gate.
        # Refuse startup until account lifecycle and current-boot recovery
        # providers are connected and verified.
        priority_enabled = self.config.get('strict_priority_enabled', False)
        if type(priority_enabled) is not bool:
            raise ValueError('strict_priority_enabled must be a boolean')
        self.config.setdefault('strict_priority_enabled', False)
        self.config.setdefault("max_job_minutes", self.config.get("max_time_minutes", 4320))
        self.config.setdefault("jobcomp_log", self.config.get("jobcomp_path", "/var/log/slurm/jobcomp.log"))
        self.config.setdefault("capacity", {"gpu": self.config.get("max_requested_gpu", 1), "cpu": self.config.get("max_cpu", os.cpu_count() or 1), "memory_mb": self.config.get("max_memory_mb", 112000)})
        if self.config.get("slurm_conf"):
            os.environ["SLURM_CONF"] = self.config["slurm_conf"]
        self.store = Store(self.config.get("state_db", "/var/lib/pro6000d-training/state.sqlite3"))
        self.default_quota = self.config.get("default_quota", {
            "gpu_limit": 1, "concurrent_gpu_limit": 1, "active_job_limit": 8,
        })
        persisted_default = self.store.get_setting("default_quota")
        if isinstance(persisted_default, dict) and persisted_default:
            self.default_quota = persisted_default
        self.qos = self.config.get("qos", {
            "debug": {"max_minutes": 60}, "normal": {"max_minutes": 1440},
            "long": {"max_minutes": 4320},
        })
        persisted_qos = self.store.get_setting("qos")
        if isinstance(persisted_qos, dict) and persisted_qos:
            self.qos = persisted_qos
        self.socket_path = self.config.get("socket_path", DEFAULT_SOCKET)
        self.admission_dir = pathlib.Path(self.config.get("admission_dir", "/var/lib/pro6000d-training/admission"))
        self.training_group = self.config.get("training_group", "pro6000d-training")
        self.admin_group = self.config.get("admin_group", "pro6000d-admin")
        self.last_tick = 0.0
        self.scheduler_error = None
        self.last_reconcile_ok = False
        self.gpu_cache = None
        self.gpu_sample_monotonic = None
        self.gpu_history = deque(maxlen=120)
        self.service_started_at = utc_now()
        columns = {r[1] for r in self.store.db.execute("PRAGMA table_info(jobs)")}
        if "client_request_id" not in columns:
            self.store.db.execute("ALTER TABLE jobs ADD COLUMN client_request_id TEXT")
        if "client_request_hash" not in columns:
            self.store.db.execute("ALTER TABLE jobs ADD COLUMN client_request_hash TEXT")
        self.store.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_request ON jobs(owner_uid,client_request_id) WHERE client_request_id IS NOT NULL")
        for name, definition in (("origin", "TEXT NOT NULL DEFAULT 'platform'"),
                                 ("source_scheduler_name", "TEXT"), ("source_submit_time", "TEXT"),
                                 ("resource_unknown", "INTEGER NOT NULL DEFAULT 0"),
                                 ("selected_tier", "TEXT"), ("cancel_target", "TEXT"),
                                 ("effective_job_digest_version", "INTEGER"), ("effective_job_digest", "TEXT"),
                                 ("policy_version", "INTEGER NOT NULL DEFAULT 1"),
                                 ("policy_digest", "TEXT"),
                                 ("priority_marker_required", "INTEGER")):
            if name not in columns:
                self.store.db.execute("ALTER TABLE jobs ADD COLUMN " + name + " " + definition)
        self.store.db.execute("CREATE INDEX IF NOT EXISTS jobs_dispatch_scan ON jobs(origin,state,selected_tier,id)")
        application_columns = {r[1] for r in self.store.db.execute("PRAGMA table_info(applications)")}
        if "policy_version" not in application_columns:
            self.store.db.execute("ALTER TABLE applications ADD COLUMN policy_version INTEGER NOT NULL DEFAULT 1")

        self.store.db.execute("""CREATE TABLE IF NOT EXISTS monthly_grant_outbox (
            application_id TEXT PRIMARY KEY, target_uid INTEGER NOT NULL, month INTEGER NOT NULL,
            gpu_seconds INTEGER NOT NULL, actor_uid INTEGER NOT NULL, state TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        self.monthly = MonthlyLedger(self)
        # No runtime is installed until controller recovery, account lifecycle,
        # marker coverage and the protected anchor are independently verified.
        self.priority_runtime = None
        self.strict_admission = None
        if priority_enabled:
            self._initialize_strict_priority()

    def _initialize_strict_priority(self):
        """Initialize and recover the opted-in controller-backed priority path."""
        from ordered_anchor_reader import read_anchor
        from ordered_priority_transport import OrderedPriorityTransport
        from priority_anchor_advance import PriorityAnchorAdvancer
        from priority_admin_store import PriorityAdminStore
        from priority_chain import AuthenticatedPrefixVerifier
        from priority_coordinator import PriorityCoordinator
        from priority_outbox import PriorityOutbox
        from priority_runtime import PriorityRuntime
        from strict_admission import StrictAdmission
        cfg = self.config.get('monthly_controller')
        if not isinstance(cfg, dict) or cfg.get('activation_enabled') is not True:
            raise RuntimeError('controller activation must be explicitly enabled')
        manifest = cfg.get('release_manifest', '/etc/pro6000d-training/monthly-release.json')
        controller_uid = pwd.getpwnam(cfg.get('controller_user', 'slurm')).pw_uid
        pid = verify_release(manifest, controller_uid, return_controller_pid=True)
        if not pid:
            raise RuntimeError('controller release attestation failed')
        anchor_dir = cfg['priority_anchor_directory']
        lock_dir = cfg['priority_lock_directory']
        anchor = read_anchor(anchor_dir)
        transport = OrderedPriorityTransport(cfg.get('socket_path', '/var/lib/slurm/ctld/time-quota.sock'),
                                             controller_uid, pid, anchor['epoch'], release_manifest=manifest,
                                             open_snapshot=True)
        bridge = transport.bridge
        initial = transport.status()
        if not bridge.readiness():
            raise RuntimeError('controller recovery is not ready')
        db = self.store.db
        db.execute('CREATE TABLE IF NOT EXISTS unix_account_generations '
                   '(uid INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, generation TEXT NOT NULL)')
        def lifecycle(user):
            # Unix account management is external. Recreating an account with
            # the same UID and name requires an administrative generation reset.
            by_name = pwd.getpwnam(user.pw_name)
            by_uid = pwd.getpwuid(user.pw_uid)
            if by_name.pw_uid != user.pw_uid or by_uid.pw_name != user.pw_name:
                raise ControllerUnavailable('Unix account identity changed')
            row = db.execute('SELECT username,generation FROM unix_account_generations WHERE uid=?', (user.pw_uid,)).fetchone()
            if row is None:
                generation = uuid.uuid4().hex
                db.execute('INSERT OR IGNORE INTO unix_account_generations VALUES(?,?,?)', (user.pw_uid,user.pw_name,generation))
                row = db.execute('SELECT username,generation FROM unix_account_generations WHERE uid=?', (user.pw_uid,)).fetchone()
            if row is None or row[0] != user.pw_name or not re.fullmatch(r'[0-9a-f]{32}', row[1]):
                raise ControllerUnavailable('Unix account generation changed')
            return row[1]
        outbox = PriorityOutbox(db)
        store = PriorityAdminStore(db, outbox)
        verifier = AuthenticatedPrefixVerifier(transport.chain_lookup, lambda: read_anchor(anchor_dir))
        coordinator = PriorityCoordinator(store, transport, lock_dir, verifier,
                                          PriorityAnchorAdvancer(anchor_dir, transport))
        def readiness():
            if verify_release(manifest, controller_uid, return_controller_pid=True) != pid or not bridge.readiness():
                return False
            status = transport.status()
            current_anchor = read_anchor(anchor_dir)
            return (status['epoch'] == initial['epoch'] and status['boot'] == initial['boot'] and
                    current_anchor['epoch'] == status['epoch'] and
                    current_anchor['sequence'] <= status['watermark'] and
                    transport.chain_lookup(status, current_anchor['sequence']) == current_anchor['hash'])
        runtime = PriorityRuntime(pwd.getpwuid, lifecycle, store, coordinator, readiness,
                                  snapshot_proof=transport.status)
        strict = StrictAdmission(runtime, bridge, db)
        ReservationCoordinator(db, bridge)
        if not readiness():
            raise RuntimeError('controller boot or anchor is not verified')
        strict.recover_pending()
        # Before creating any BASE/user history, attempt the durable genesis
        # cutover. Existing active workloads make the attempt fail closed.
        try:
            runtime.cutover_ready = strict.try_quiescent_generation_cutover()
        except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError):
            runtime.cutover_ready = False
        self.priority_runtime, self.strict_admission = runtime, strict
        if runtime.cutover_ready:
            self._enroll_priority_accounts()

    def _enroll_priority_accounts(self):
        for user in pwd.getpwall():
            if (self.group_membership(self.admin_group, user) or
                    self.group_membership(self.training_group, user)):
                self.priority_runtime.enroll_account(user.pw_uid, user.pw_name)

    # ---------- identity and authorization ----------
    def peer_uid(self, conn: socket.socket) -> int:
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        if len(raw) < struct.calcsize("3i"):
            raise ApiError(401, "peer_credentials_unavailable", "无法取得 Cockpit bridge 的 Unix peer credentials")
        # Linux ucred is struct ucred { pid_t pid; uid_t uid; gid_t gid; }.
        return struct.unpack("3i", raw[:struct.calcsize("3i")])[1]

    def user(self, uid: int) -> pwd.struct_passwd:
        try:
            return pwd.getpwuid(uid)
        except KeyError:
            raise ApiError(403, "unknown_identity", "调用 UID 不对应本机用户")

    def group_membership(self, group_name: str, user: pwd.struct_passwd) -> bool:
        try:
            group = grp.getgrnam(group_name)
        except KeyError:
            return False
        return user.pw_name in group.gr_mem or user.pw_gid == group.gr_gid or group.gr_gid in os.getgrouplist(user.pw_name, user.pw_gid)

    def identity(self, uid: int) -> Dict[str, Any]:
        user = self.user(uid)
        admin = self.group_membership(self.admin_group, user)
        training = self.group_membership(self.training_group, user)
        if not (admin or training):
            raise ApiError(403, "not_authorized", "本机账户未加入训练管理允许组")
        return {"uid": uid, "username": user.pw_name, "home": user.pw_dir, "is_admin": admin, "is_training_user": training}

    def require_admin(self, ident: Dict[str, Any]) -> None:
        if not ident["is_admin"]:
            raise ApiError(403, "admin_required", "需要本机管理员组权限")

    # ---------- quota and filesystem policy ----------
    def quota(self, uid: int) -> Dict[str, Any]:
        row = self.store.db.execute("SELECT * FROM quota_overrides WHERE uid=?", (uid,)).fetchone()
        value = dict(row) if row else self.default_quota
        jobs = int(value.get("concurrent_job_limit", value.get("concurrent_gpu_limit", 1)))
        return {"gpu_limit": int(value["gpu_limit"]), "concurrent_job_limit": jobs,
                "concurrent_gpu_limit": jobs, "active_job_limit": int(value["active_job_limit"]),
                "source": "personal_override" if row else "default"}

    def quota_value(self, value: Any) -> Dict[str, int]:
        if not isinstance(value, dict):
            raise ApiError(400, "invalid_quota", "配额必须是对象")
        try:
            result = {"gpu_limit": int(value["gpu_limit"]),
                      "concurrent_gpu_limit": int(value.get("concurrent_job_limit", value.get("concurrent_gpu_limit", 1))),
                      "active_job_limit": int(value["active_job_limit"])}
        except (KeyError, TypeError, ValueError):
            raise ApiError(400, "invalid_quota", "配额字段必须是整数")
        if min(result.values()) < 1 or max(result.values()) > 1024:
            raise ApiError(400, "invalid_quota", "配额必须在 1 到 1024 之间")
        if result["concurrent_gpu_limit"] > result["active_job_limit"]:
            raise ApiError(400, "invalid_quota", "并发任务上限不能超过活动任务上限")
        return result

    def accounts(self, ident: Dict[str, Any]) -> List[Dict[str, Any]]:
        users = []
        for user in pwd.getpwall():
            if not ident["is_admin"] and user.pw_uid != ident["uid"]:
                continue
            try:
                who = self.identity(user.pw_uid)
            except ApiError:
                continue
            users.append({**who, "quota": self.quota(user.pw_uid), "usage": self.active_usage(user.pw_uid), "monthly_quota": self.monthly_controller_snapshot(user.pw_uid)})
        return sorted(users, key=lambda x: x["uid"])

    def user_roots(self, user: pwd.struct_passwd) -> List[pathlib.Path]:
        roots = self.config.get("user_roots", ["/home/{username}", "/srv/pro6000d/users/{username}"])
        return [pathlib.Path(x.format(username=user.pw_name, uid=user.pw_uid)).resolve() for x in roots]

    def safe_user_path(self, value: str, user: pwd.struct_passwd, must_exist: bool = True) -> str:
        if not isinstance(value, str) or not value or "\x00" in value or any(ord(ch) < 32 for ch in value):
            raise ApiError(400, "invalid_path", "路径不能为空且不能包含 NUL")
        path = pathlib.Path(value)
        if not path.is_absolute():
            raise ApiError(400, "invalid_path", "工作目录和日志路径须为绝对路径")
        try:
            resolved = path.resolve(strict=must_exist)
        except FileNotFoundError:
            raise ApiError(400, "path_not_found", "路径不存在")
        if not any(resolved == root or root in resolved.parents for root in self.user_roots(user)):
            raise ApiError(403, "path_outside_user_root", "工作目录和输出路径必须位于本用户允许目录")
        try:
            st = resolved.stat()
        except OSError as exc:
            raise ApiError(400, "path_unreadable", str(exc))
        if st.st_uid not in (user.pw_uid, 0):
            raise ApiError(403, "path_owner_mismatch", "路径不属于调用用户")
        return str(resolved)

    @staticmethod
    def normalize_legacy_defaults(payload):
        if not isinstance(payload, dict):
            raise ApiError(400, 'invalid_job', '任务必须是对象')
        if 'time_minutes' in payload or 'qos' in payload:
            raise ApiError(400, 'legacy_time_fields', '新任务不支持指定时限或 QoS，请移除参数并更新客户端')
        return payload, False

    def validate_job(self, ident: Dict[str, Any], payload: Dict[str, Any], temporary: bool = False, policy_version: Optional[int] = None) -> Dict[str, Any]:
        user = self.user(ident["uid"])
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_job", "任务必须是对象")
        if policy_version is None:
            payload, _ = self.normalize_legacy_defaults(payload)
            policy_version = 2
        selected_tier = payload.get("selected_tier")
        readiness = self.monthly_controller_snapshot(ident["uid"])
        ready = readiness.get("enforcement_ready") is True and readiness.get("selectable_tiers_ready") is True
        if policy_version == 2 and "selected_tier" not in payload:
            raise ApiError(400, "selected_tier_required", "请选择月度额度档位")
        cfg = self.config.get("monthly_controller", {})
        configured = isinstance(cfg, dict) and cfg.get("activation_enabled") is True
        if configured and not ready:
            raise ApiError(503, "monthly_gate_unavailable", "月度额度控制暂不可用，请稍后重试")
        if "selected_tier" in payload:
            if not isinstance(selected_tier, str) or selected_tier not in ("debug", "important", "normal"):
                raise ApiError(400, "invalid_selected_tier", "请选择有效的月度额度档位")
            if not ready:
                raise ApiError(409, "selected_tier_unavailable", "月度选档控制尚未就绪")
        elif ready:
            raise ApiError(400, "selected_tier_required", "请选择月度额度档位")
        name = payload.get("name", "training")
        command = payload.get("command", "")
        if policy_version not in (1, 2):
            raise ApiError(409, "invalid_policy", "任务计费版本无效")
        qos = payload.get("qos", self.config.get("unlimited_qos", "long")) if policy_version == 2 else payload.get("qos", "normal")
        if policy_version == 2 and (not isinstance(qos, str) or not SAFE_NAME.fullmatch(qos)):
            raise ApiError(503, "invalid_unlimited_qos", "无限时长 QoS 配置无效")
        try:
            gpu = int(payload.get("gpu", 1)); cpu = int(payload.get("cpu", 1))
            mem = int(payload.get("memory_mb", 1024)); minutes = 4294967295 if policy_version == 2 else int(payload.get("time_minutes", 60))
        except (TypeError, ValueError):
            raise ApiError(400, "invalid_resource", "GPU、CPU、内存和时限必须是整数")
        if not SAFE_NAME.fullmatch(str(name)):
            raise ApiError(400, "invalid_name", "任务名只允许字母、数字、点、下划线和连字符")
        if not isinstance(command, str) or not command.strip() or len(command) > 4096 or "\x00" in command:
            raise ApiError(400, "invalid_command", "命令不能为空、不能超过 4096 字符或包含 NUL")
        if policy_version == 1 and qos not in self.qos:
            raise ApiError(400, "invalid_qos", "未知时限档位")
        if gpu < 1 or gpu > int(self.config.get("max_requested_gpu", 64)) or cpu < 1 or cpu > 512 or mem < 128 or mem > 1048576:
            raise ApiError(400, "invalid_resource", "资源请求超出接口允许范围")
        if policy_version == 1 and (minutes < 1 or minutes > int(self.config.get("max_job_minutes", 4320))):
            raise ApiError(400, "cluster_time_limit", "时限超出测试节点允许的最大值")
        if policy_version == 1 and not temporary and minutes > int(self.qos[qos]["max_minutes"]):
            raise ApiError(400, "time_limit_exceeded", "实际时限超过所选档位的单作业上限")
        capacity = self.config.get("capacity", {})
        if gpu > int(capacity.get("gpu", self.config.get("max_requested_gpu", 64))) or cpu > int(capacity.get("cpu", os.cpu_count() or 1)) or mem > int(capacity.get("memory_mb", 1048576)):
            raise ApiError(409, "machine_unsatisfiable", "请求超过本测试节点的 GPU、CPU 或内存容量")
        workdir = self.safe_user_path(payload.get("workdir", ""), user)
        if not pathlib.Path(workdir).is_dir():
            raise ApiError(400, "not_directory", "工作目录必须是目录")
        output = payload.get("output_path", "")
        if not output:
            output = os.path.join(workdir, "slurm-%j.out")
        if not isinstance(output, str) or "\x00" in output or any(ord(ch) < 32 for ch in output):
            raise ApiError(400, "invalid_output_path", "日志路径不能包含控制字符")
        if any(output[i:i + 2] != "%j" for i, ch in enumerate(output) if ch == "%") or output.count("%j") > 1:
            raise ApiError(400, "invalid_output_path", "日志路径只允许一个 %j 作业号占位符")
        output_path = pathlib.Path(output)
        if "%j" in str(output_path.parent):
            raise ApiError(400, "invalid_output_path", "%j 只能出现在日志文件名中")
        output_parent_dir = self.safe_user_path(str(output_path.parent), user)
        output_name = output_path.name
        if output_name in ("", ".", "..") or "/" in output_name or "\\" in output_name:
            raise ApiError(400, "invalid_output_path", "日志文件名无效")
        candidate = pathlib.Path(output_parent_dir) / output_name
        if candidate.exists() and candidate.is_symlink():
            raise ApiError(400, "invalid_output_path", "不能使用符号链接作为日志文件")
        output = str(candidate)
        env_name = str(payload.get("environment", "system"))
        if env_name != "system" and not SAFE_ENV.fullmatch(env_name):
            raise ApiError(400, "invalid_environment", "环境名只能包含安全路径字符")
        return {"name": str(name), "command": command.strip(), "qos": qos, "gpu": gpu, "cpu": cpu,
                "memory_mb": mem, "time_minutes": minutes, "workdir": workdir,
                "output_path": output, "environment": env_name,
                **({"selected_tier": selected_tier} if "selected_tier" in payload else {})}

    def active_usage(self, uid: int) -> Dict[str, int]:
        rows = self.store.db.execute("SELECT requested_gpu,state FROM jobs WHERE owner_uid=?", (uid,)).fetchall()
        active = [r for r in rows if r["state"] in ACTIVE_STATES]
        reserved = [r for r in active if r["state"] != "WAITING_QUOTA"]
        running = [r for r in active if r["state"] in ("RUNNING", "COMPLETING", "SUSPENDED")]
        return {"gpu": sum(r["requested_gpu"] for r in reserved), "concurrent_gpu": sum(r["requested_gpu"] for r in reserved),
                "active_jobs": len(active), "concurrent_jobs": len(reserved), "running_jobs": len(running),
                "running_gpu": sum(r["requested_gpu"] for r in running)}

    def scheduler_available(self) -> bool:
        return shutil.which("sbatch") is not None and shutil.which("scontrol") is not None

    def admission_available(self) -> bool:
        if not self.config.get("admission_plugin", False):
            return False
        plugin = self.config.get("admission_plugin_name", "lua")
        if not isinstance(plugin, str) or not re.fullmatch(r"[A-Za-z0-9_]+", plugin):
            return False
        path = pathlib.Path(self.config.get("admission_plugin_path", "/etc/slurm-llnl/job_submit.lua") if plugin == "lua" else self.config.get("admission_plugin_binary", "/nonexistent"))
        try:
            st = path.lstat()
            if st.st_uid != 0 or st.st_mode & 0o022 or not stat.S_ISREG(st.st_mode) or st.st_size > 32 * 1024 * 1024:
                return False
            if plugin == "lua":
                if "PRO6000D_ROOT_BROKER_V1" not in path.read_text():
                    return False
            else:
                expected = self.config.get("admission_plugin_sha256", "")
                if not re.fullmatch(r"[a-fA-F0-9]{64}", expected) or hashlib.sha256(path.read_bytes()).hexdigest() != expected.lower():
                    return False
            code, out, _ = run(["scontrol", "show", "config"], timeout=5)
            match = re.search(r"(?m)^\s*JobSubmitPlugins\s*=\s*(\S+)", out)
            return code == 0 and bool(match) and plugin in match.group(1).split(",")
        except (OSError, UnicodeError):
            return False

    @staticmethod
    def slurm_fields(line: str) -> Dict[str, str]:
        # Values such as JobName and WorkDir may contain spaces.
        return {m.group(1): m.group(2).strip() for m in re.finditer(r"(?:^| )([A-Za-z][A-Za-z0-9_:/]*)=(.*?)(?= [A-Za-z][A-Za-z0-9_:/]*=|$)", line)}

    def import_external_jobs(self, records: Dict[str, Any]) -> None:
        if not self.config.get("import_external_jobs", False):
            return
        for sid, rec in records.items():
            if rec["state"] not in ("RUNNING", "PENDING", "COMPLETING", "SUSPENDED", "CONFIGURING", "RESIZING", "REQUEUED", "REQUEUE_FED"):
                continue
            f = rec["fields"]
            uid_match = re.search(r"\((\d+)\)$", f.get("UserId", ""))
            if not uid_match or int(uid_match.group(1)) == 0:
                continue
            uid = int(uid_match.group(1))
            try:
                ident = self.identity(uid)
            except (ApiError, KeyError):
                continue
            if not ident["is_training_user"]:
                continue
            # Never import a broker job a second time, including uncertain submits.
            if self.store.db.execute("SELECT 1 FROM jobs WHERE origin='platform' AND (scheduler_id=? OR ?='p6-'||id)", (sid,rec["name"])).fetchone():
                continue
            submitted = f.get("SubmitTime", "")
            if not submitted or submitted in ("Unknown", "N/A"):
                # Stable identity is required before persistent job-ID reconciliation.
                self.scheduler_error = "存在提交时间未知的外部任务；暂停新任务，等待管理员核对"
                self.last_reconcile_ok = False
                continue
            jid = "external-slurm-" + sid + "-" + hashlib.sha256((str(uid)+"|"+submitted).encode()).hexdigest()[:16]
            tres = dict(item.split("=",1) for item in (f.get("AllocTRES") or f.get("ReqTRES") or "").split(",") if "=" in item)
            requested = dict(item.split("=",1) for item in f.get("ReqTRES", "").split(",") if "=" in item)
            tres = {**requested, **tres}
            gpu = tres.get("gres/gpu")
            if gpu is None:
                typed = [v for k,v in tres.items() if k.startswith("gres/gpu:")]
                gpu = str(sum(int(v) for v in typed)) if typed and all(v.isdigit() for v in typed) else None
            unknown = gpu is None or not gpu.isdigit()
            gpu_count = int(self.config["capacity"]["gpu"]) if unknown else int(gpu)
            cpu = f.get("NumCPUs", tres.get("cpu", ""))
            cpu_count = int(cpu) if cpu.isdigit() else int(self.config["capacity"]["cpu"])
            mem = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGT]?)", tres.get("mem", ""), re.I)
            memory = max(1,int(float(mem.group(1))*{"":1,"K":1/1024,"M":1,"G":1024,"T":1048576}[mem.group(2).upper()])) if mem else int(self.config["capacity"]["memory_mb"])
            duration = re.fullmatch(r"(?:(\d+)-)?(\d+):(\d+):(\d+)", f.get("TimeLimit", ""))
            minutes = (int(duration.group(1) or 0)*1440 + int(duration.group(2))*60 + int(duration.group(3)) + (1 if int(duration.group(4)) else 0)) if duration else int(self.config["max_job_minutes"])
            state = rec["state"] if rec["state"] in ACTIVE_STATES else "PENDING"
            now = utc_now()
            reason = "外部 Slurm 任务，仅查看；GPU 数量未知，按整机保守预留" if unknown else "外部 Slurm 任务，仅查看；" + (rec["reason"] or state)
            values = {"id":jid,"scheduler_id":sid,"owner_uid":uid,"owner_name":ident["username"],"name":rec["name"],"state":state,
                      "requested_gpu":gpu_count,"requested_cpu":cpu_count,"requested_mem_mb":memory,"requested_minutes":minutes,
                      "qos":f.get("QOS", "unknown"),"workdir":f.get("WorkDir", ""),"env_name":"external","command":"",
                      "output_path":"","reason_code":"external_resources_unknown" if unknown else "external_read_only","reason_text":reason,
                      "created_at":now,"updated_at":now,"origin":"external","source_scheduler_name":rec["name"],
                      "source_submit_time":submitted,"resource_unknown":int(unknown)}
            # Refresh resource reservations if an existing native job is resized.
            updates = ("state", "requested_gpu", "requested_cpu", "requested_mem_mb", "requested_minutes", "qos", "resource_unknown", "updated_at")
            self.store.db.execute("INSERT INTO jobs ("+",".join(values)+") VALUES ("+",".join("?" for _ in values)+") ON CONFLICT(id) DO UPDATE SET "+",".join(key+"=excluded."+key for key in updates),tuple(values.values()))

    @staticmethod
    def _held_top_record_matches(row: sqlite3.Row, record: Optional[Dict[str, Any]]) -> bool:
        if not record or record.get('name') != 'p6-' + row['id'] or record.get('state', '').split('+')[0] != 'PENDING':
            return False
        fields = record.get('fields', {})
        user = fields.get('UserId', '')
        uid_match = re.fullmatch(r'.*\(([0-9]+)\)', user)
        uid_text = uid_match.group(1) if uid_match else user
        expected_comment = 'pro6000d-job:' + row['id'] + ':tier=' + str(row['selected_tier'])
        priority = fields.get('Priority', '')
        return (uid_text.isdigit() and int(uid_text) == row['owner_uid'] and priority.isdigit() and
                int(priority) == 0 and record.get('reason') in ('JobHeldUser', 'JobHeldAdmin') and
                fields.get('Comment') == expected_comment)

    def _reconcile_missing_ordinary(self, row, strict):
        db = self.store.db
        sql = "UPDATE jobs SET reason_code='completion_unknown',reason_text='调度器已无记录，等待管理员核对；保留额度，避免重复提交',updated_at=? WHERE id=?"
        if strict is None or row['origin'] != 'platform':
            db.execute(sql, (utc_now(), row['id']))
            return True
        with strict.runtime.coordinator.serialized():
            db.execute('BEGIN IMMEDIATE')
            try:
                current = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
                if (current is None or tuple(current) != tuple(row) or strict._has_marker_locked(row['id']) or
                        not strict._verify_effective_job(row['owner_uid'], row['id'], current['effective_job_digest'])):
                    db.execute('ROLLBACK')
                    return False
                db.execute(sql, (utc_now(), row['id']))
                db.execute('COMMIT')
                return True
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def _reconcile_set_scheduler_id(self, row, sid, top_marker, strict):
        """Persist a candidate ID only if the queued identity is unchanged."""
        db = self.store.db
        sql = ("UPDATE jobs SET scheduler_id=? WHERE id=? AND state='SUBMITTING' AND scheduler_id IS NULL"
               if top_marker else "UPDATE jobs SET scheduler_id=? WHERE id=?")
        if strict is None or row['origin'] != 'platform':
            return db.execute(sql, (sid, row['id'])).rowcount == 1
        with strict.runtime.coordinator.serialized():
            db.execute('BEGIN IMMEDIATE')
            try:
                current = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
                if (current is None or tuple(current) != tuple(row) or
                        strict._has_marker_locked(row['id']) is not top_marker or
                        not strict._verify_effective_job(row['owner_uid'], row['id'], current['effective_job_digest'])):
                    db.execute('ROLLBACK')
                    return False
                changed = db.execute(sql, (sid, row['id'])).rowcount == 1
                db.execute('COMMIT')
                return changed
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def _reconcile_monthly_reservation(self, row, sid, final, top_marker, strict):
        """Recheck a platform reservation under the priority lock when enabled.

        Controller boot is checked around the operation. A changed boot or
        unknown reply leaves the durable reservation for later reconciliation;
        this is not proof that the scheduler's earlier snapshot is current.
        """
        if strict is None or row['origin'] != 'platform':
            coordinator = self.monthly_reservation_coordinator()
            if final:
                coordinator.finish(row['id'], row['owner_uid'], int(sid))
            else:
                coordinator.bind(row['id'], row['owner_uid'], int(sid))
            return
        db = self.store.db
        with strict.runtime.coordinator.serialized():
            if db.in_transaction or not strict.runtime.reconciled_for_current_boot():
                raise ReservationUncertain('priority prerequisites unavailable')
            expected = dict(row)
            if expected['scheduler_id'] not in (None, sid):
                raise ReservationUncertain('scheduler identity changed')
            expected['scheduler_id'] = sid
            current = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
            if (current is None or dict(current) != expected or
                    strict._has_marker_locked(row['id']) is not top_marker or
                    not strict._verify_effective_job(row['owner_uid'], row['id'], current['effective_job_digest'])):
                raise ReservationUncertain('task changed before reservation reconciliation')
            strict._same_controller()
            transport = strict.runtime.coordinator.transport
            before = transport.status()
            source = self.verified_monthly_bridge()
            peer = strict.bridge
            if (source.path, source.controller_uid, source.expected_pid, source.release_manifest) != (
                    peer.path, peer.controller_uid, peer.expected_pid, peer.release_manifest):
                raise ReservationUncertain('reservation controller differs from priority controller')
            pinned = PinnedControllerBridge(source.path, source.controller_uid, source.expected_pid,
                                            source.timeout, release_manifest=source.release_manifest)
            if not pinned.readiness():
                raise ReservationUncertain('reservation controller unavailable')
            coordinator = ReservationCoordinator(db, pinned)
            if final:
                coordinator.finish(row['id'], row['owner_uid'], int(sid), require_controller_confirmation=True)
            else:
                coordinator.bind(row['id'], row['owner_uid'], int(sid))
            after = transport.status()
            if (any(after.get(k) != before.get(k) for k in ('epoch', 'boot', 'watermark')) or
                    not strict.runtime.reconciled_for_current_boot() or
                    (top_marker and 'fenced' in after and after['fenced'] is not True)):
                raise ReservationUncertain('controller changed during reservation reconciliation')
            current = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
            if (current is None or dict(current) != expected or
                    strict._has_marker_locked(row['id']) is not top_marker or
                    not strict._verify_effective_job(row['owner_uid'], row['id'], current['effective_job_digest'])):
                raise ReservationUncertain('task changed during reservation reconciliation')

    def _reconcile_set_ordinary_state(self, row, sid, state, reason, final, strict, *, reason_text=None):
        sql = ("UPDATE jobs SET state=?,reason_code=?,reason_text=?,updated_at=?,"
               "started_at=COALESCE(started_at,?),finished_at=? WHERE id=?")
        now = utc_now()
        params = (state, reason, reason_text if reason_text is not None else (reason or state), now, now if state == 'RUNNING' else None,
                  now if final else None, row['id'])
        db = self.store.db
        if strict is None or row['origin'] != 'platform':
            return db.execute(sql, params).rowcount == 1
        expected = dict(row)
        if expected['scheduler_id'] not in (None, sid):
            return False
        expected['scheduler_id'] = sid
        with strict.runtime.coordinator.serialized():
            db.execute('BEGIN IMMEDIATE')
            try:
                current = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
                if (current is None or dict(current) != expected or
                        strict._has_marker_locked(row['id']) is not False or
                        not strict._verify_effective_job(row['owner_uid'], row['id'], current['effective_job_digest'])):
                    db.execute('ROLLBACK')
                    return False
                changed = db.execute(sql, params).rowcount == 1
                db.execute('COMMIT')
                return changed
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def _reconcile_set_top_held(self, row, sid, receipt, strict):
        if (not isinstance(receipt, dict) or type(receipt.get('job_id')) is not int or receipt['job_id'] != int(sid) or
                type(receipt.get('submit')) is not int or not 0 < receipt['submit'] <= 2**63-1 or
                type(receipt.get('sluid')) is not int or not 0 < receipt['sluid'] <= 2**64-1 or
                type(receipt.get('restart')) is not int or receipt['restart'] != 0 or
                type(receipt.get('generation')) is not int or receipt['generation'] != 0):
            raise ControllerUnavailable('priority binding receipt unavailable')
        expected = dict(row)
        if expected['scheduler_id'] not in (None, sid):
            return False
        expected['scheduler_id'] = sid
        db = self.store.db
        with strict.runtime.coordinator.serialized():
            db.execute('BEGIN IMMEDIATE')
            try:
                current = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
                binding = db.execute('SELECT uid,scheduler_id,submit_time,sluid,restart,generation '
                                     'FROM top_marker_bindings WHERE marker=?', (row['id'],)).fetchone()
                if (current is None or dict(current) != expected or
                        strict._has_marker_locked(row['id']) is not True or binding is None or
                        tuple(binding) != (row['owner_uid'], int(sid), receipt['submit'], str(receipt['sluid']), 0, 0) or
                        not isinstance(current['effective_job_digest'], str) or
                        not re.fullmatch(r'[0-9a-f]{64}', current['effective_job_digest']) or
                        not strict._verify_effective_job(row['owner_uid'], row['id'], current['effective_job_digest'])):
                    db.execute('ROLLBACK')
                    return False
                changed = db.execute("UPDATE jobs SET state='PENDING',reason_code='priority_pending',"
                    "reason_text='优先任务已绑定，等待调度',updated_at=? "
                    "WHERE id=? AND state IN ('SUBMITTING','PENDING') AND scheduler_id=?",
                    (utc_now(), row['id'], sid)).rowcount == 1
                db.execute('COMMIT')
                return changed
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def _reconcile_note_top_unknown(self, row, sid, code, message, strict):
        expected = dict(row)
        if expected['scheduler_id'] not in (None, sid):
            return False
        expected['scheduler_id'] = sid
        db = self.store.db
        with strict.runtime.coordinator.serialized():
            db.execute('BEGIN IMMEDIATE')
            try:
                current = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
                if (current is None or dict(current) != expected or
                        strict._has_marker_locked(row['id']) is not True):
                    db.execute('ROLLBACK')
                    return False
                changed = db.execute('UPDATE jobs SET reason_code=?,reason_text=?,updated_at=? '
                                     'WHERE id=? AND state=? AND scheduler_id IS ?',
                                     (code, message, utc_now(), row['id'], row['state'], sid)).rowcount == 1
                db.execute('COMMIT')
                return changed
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def _reconcile_ordinary_terminal(self, row, sid, strict, accounting):
        """Consume an exact settled controller receipt and matching completion record."""
        states = {3: 'COMPLETED', 4: 'CANCELLED', 5: 'FAILED', 6: 'FAILED', 7: 'FAILED', 8: 'FAILED', 9: 'FAILED', 10: 'FAILED', 11: 'FAILED'}
        with strict.runtime.coordinator.serialized():
            if not strict.runtime.reconciled_for_current_boot() or strict._has_marker_locked(row['id']):
                raise ControllerUnavailable('ordinary priority identity unavailable')
            strict._same_controller()
            before = strict.runtime.coordinator.transport.status()
            legacy_hold = self.store.db.execute('SELECT uid,scheduler_id,state,binding_version,bind_submit,bind_sluid FROM monthly_dispatch_holds WHERE job_id=?', (row['id'],)).fetchone()
            legacy = legacy_hold is not None and tuple(legacy_hold) == (row['owner_uid'], int(sid), 'BOUND', 1, None, None)
            receipt = (strict.bridge.legacy_terminal_receipt(row['owner_uid'], row['id'], int(sid)) if legacy else
                       strict.bridge.ordinary_terminal_receipt(row['owner_uid'], row['id'], int(sid)))
            status = strict.reservation_status_for_job(row['owner_uid'], row['id'])
            terminal = states.get(receipt['state'] & 0xff)
            if (terminal is None or status.get('state') != 'released' or
                    status.get('scheduler_id') != int(sid) or (not legacy and status.get('sluid') != receipt['sluid']) or
                    status.get('generation') != receipt['generation'] or
                    strict.runtime.coordinator.transport.status() != before or
                    not strict.runtime.reconciled_for_current_boot()):
                raise ControllerUnavailable('ordinary terminal evidence not confirmed')
            db = self.store.db
            db.execute('BEGIN IMMEDIATE')
            try:
                current = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
                held = db.execute('SELECT uid,scheduler_id,state,binding_version,bind_submit,bind_sluid '
                                  'FROM monthly_dispatch_holds WHERE job_id=?', (row['id'],)).fetchone()
                if (current is None or dict(current) != dict(row) or held is None or
                        tuple(held) != ((row['owner_uid'], int(sid), 'BOUND', 1, None, None) if legacy else (row['owner_uid'], int(sid), 'BOUND', 2, str(receipt['submit']), str(receipt['sluid']))) or
                        strict._has_marker_locked(row['id']) or
                        not strict._verify_effective_job(row['owner_uid'], row['id'], row['effective_job_digest'])):
                    raise ControllerUnavailable('ordinary terminal identity changed')
                if db.execute("UPDATE monthly_dispatch_holds SET state='CLOSED' WHERE job_id=? AND state='BOUND' AND scheduler_id=?",
                              (row['id'], int(sid))).rowcount != 1:
                    raise ControllerUnavailable('ordinary reservation changed')
                now = utc_now()
                if db.execute("UPDATE jobs SET state=?,reason_code=?,reason_text=?,updated_at=?,finished_at=? "
                              "WHERE id=? AND scheduler_id=? AND state IN ('SUBMITTING','PENDING','RUNNING','COMPLETING','SUSPENDED')",
                              (terminal, 'slurm_state_%d_exit_%d' % (receipt['state'], receipt['exit_code']), terminal, now, now, row['id'], sid)).rowcount != 1:
                    raise ControllerUnavailable('ordinary task changed')
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def _reconcile_pre_cutover_legacy_terminal(self, row, strict):
        """Close a proven legacy allocation without inventing priority identity.

        The controller receipt requires a version-1 RELEASE2 and verified node
        cleanup.  An absent receipt includes still-running and unknown jobs.
        """
        sid = row['scheduler_id']
        if (row['origin'] != 'platform' or not isinstance(sid, str) or
                not re.fullmatch(r'[1-9][0-9]{0,9}', sid) or int(sid) > 2**32-1 or
                row['priority_marker_required'] not in (None, 0)):
            raise ControllerUnavailable('legacy task identity unavailable')
        db = self.store.db
        with strict.runtime.coordinator.serialized():
            if strict.runtime.cutover_ready or not strict.runtime.reconciled_for_current_boot():
                raise ControllerUnavailable('legacy recovery context changed')
            strict._same_controller()
            before = strict.runtime.coordinator.transport.status()
            hold = db.execute('SELECT * FROM monthly_dispatch_holds WHERE job_id=?', (row['id'],)).fetchone()
            if (hold is None or hold['uid'] != row['owner_uid'] or
                    hold['scheduler_id'] != int(sid) or hold['state'] != 'BOUND' or
                    hold['binding_version'] != 1 or hold['bind_submit'] is not None or
                    hold['bind_sluid'] is not None or
                    db.execute('SELECT 1 FROM top_pending_intents WHERE marker=? UNION ALL '
                               'SELECT 1 FROM top_fence_intents WHERE marker=? LIMIT 1',
                               (row['id'], row['id'])).fetchone()):
                raise ControllerUnavailable('legacy reservation or priority identity ambiguous')
            receipt = strict.bridge.legacy_terminal_receipt(row['owner_uid'], row['id'], int(sid))
            remote = strict.reservation_status_for_job(row['owner_uid'], row['id'])
            terminal = {3: 'COMPLETED', 4: 'CANCELLED', 5: 'FAILED', 6: 'FAILED',
                        7: 'FAILED', 8: 'FAILED', 9: 'FAILED', 10: 'FAILED',
                        11: 'FAILED'}.get(receipt['state'] & 0xff)
            if (terminal is None or not remote.get('present') or remote.get('state') != 'released' or
                    remote.get('scheduler_id') != int(sid) or
                    remote.get('generation') != receipt['generation'] or
                    any(remote.get(k) != hold[k] for k in ('uid', 'tier', 'gpu', 'minutes')) or
                    type(remote.get('month')) is not int or type(hold['month']) is not int or
                    not 197001 <= hold['month'] <= remote['month'] <= int(month_now().replace('-', '')) or
                    not 1 <= hold['month'] % 100 <= 12 or not 1 <= remote['month'] % 100 <= 12 or
                    strict.runtime.coordinator.transport.status() != before or
                    not strict.runtime.reconciled_for_current_boot()):
                raise ControllerUnavailable('legacy terminal proof differs from reservation')
            db.execute('BEGIN IMMEDIATE')
            try:
                current = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
                current_hold = db.execute('SELECT * FROM monthly_dispatch_holds WHERE job_id=?', (row['id'],)).fetchone()
                if (current is None or dict(current) != dict(row) or current_hold is None or
                        dict(current_hold) != dict(hold) or
                        db.execute('SELECT 1 FROM top_pending_intents WHERE marker=? UNION ALL '
                                   'SELECT 1 FROM top_fence_intents WHERE marker=? LIMIT 1',
                                   (row['id'], row['id'])).fetchone()):
                    raise ControllerUnavailable('legacy task changed during settlement')
                if db.execute("UPDATE monthly_dispatch_holds SET state='CLOSED',month=? WHERE job_id=? AND uid=? "
                              "AND scheduler_id=? AND state='BOUND' AND binding_version=1 AND month=?",
                              (remote['month'], row['id'], row['owner_uid'], int(sid), hold['month'])).rowcount != 1:
                    raise ControllerUnavailable('legacy reservation changed during settlement')
                now = utc_now()
                if db.execute("UPDATE jobs SET state=?,reason_code=?,reason_text=?,updated_at=?,finished_at=? "
                              "WHERE id=? AND owner_uid=? AND scheduler_id=? AND state=?",
                              (terminal, 'slurm_state_%d_exit_%d' % (receipt['state'], receipt['exit_code']),
                               terminal, now, now, row['id'], row['owner_uid'], sid, row['state'])).rowcount != 1:
                    raise ControllerUnavailable('legacy task changed during settlement')
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def reconcile(self) -> None:
        self.last_reconcile_ok = False
        strict = getattr(self, 'strict_admission', None)
        if strict is not None:
            if not strict.runtime.cutover_ready:
                try:
                    ready = strict.try_quiescent_generation_cutover()
                    with strict.runtime.coordinator.serialized():
                        strict.runtime.cutover_ready = ready
                    if ready:
                        self._enroll_priority_accounts()
                except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError):
                    with strict.runtime.coordinator.serialized():
                        strict.runtime.cutover_ready = False
            try:
                if strict.runtime.cutover_ready:
                    strict.migrate_proven_legacy_markers()
            except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError):
                self.scheduler_error = '旧任务身份待核对'
                return
        code, out, err = run(["scontrol", "show", "jobs", "-o"], timeout=8)
        if code:
            self.scheduler_error = err or "调度器不可达"
            return
        self.scheduler_error = None
        unresolved = False
        def top_unknown(row, sid, code, message):
            nonlocal unresolved
            unresolved = True
            self.scheduler_error = self.scheduler_error or message
            strict = getattr(self, 'strict_admission', None)
            if strict is not None:
                try:
                    self._reconcile_note_top_unknown(row, sid, code, message, strict)
                except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError):
                    pass  # Keep dispatch closed; the row may have changed.
        records = {}
        for line in out.splitlines():
            fields = self.slurm_fields(line)
            sid = fields.get("JobId", "")
            if sid.isdigit():
                records[sid] = {"state": fields.get("JobState", ""), "reason": fields.get("Reason", ""), "name": fields.get("JobName", ""), "comment": fields.get("Comment", ""), "line": line, "fields": fields}
        self.import_external_jobs(records)
        if self.scheduler_error:
            # Import may have detected a job whose stable scheduler identity
            # is unknown. Do not overwrite that failure at the end of the pass.
            unresolved = True
        complete = self.completion_records()
        strict = getattr(self, 'strict_admission', None)
        if strict is not None:
            retries = self.store.db.execute("SELECT c.uid,c.marker,c.scheduler_id FROM top_submitted_cancel_intents c JOIN jobs j ON j.id=c.marker WHERE c.state IN ('requested','reservation_cancelled') AND j.state='PENDING'").fetchall()
            for cancel_uid, cancel_marker, cancel_sid in retries:
                try:
                    strict.cancel_submitted_pending(cancel_uid, cancel_marker, cancel_sid)
                except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError, ValueError):
                    unresolved = True
                    self.scheduler_error = self.scheduler_error or '优先任务取消待核对'
        rows = self.store.db.execute("SELECT * FROM jobs WHERE state IN ('SUBMITTING','PENDING','RUNNING','COMPLETING','SUSPENDED')").fetchall()
        for row in rows:
            sid = row["scheduler_id"]
            strict = getattr(self, 'strict_admission', None)
            if strict is not None and not strict.runtime.cutover_ready and row['origin'] == 'platform':
                try:
                    self._reconcile_pre_cutover_legacy_terminal(row, strict)
                except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError, ValueError):
                    # Running, unbound, marked, and uncertain jobs are never mutated here.
                    unresolved = True
                    self.scheduler_error = self.scheduler_error or '旧任务终止证明待核对'
                continue
            try:
                top_marker = strict is not None and row['origin'] == 'platform' and strict.has_marker(row['id'])
            except (ControllerUnavailable, OSError):
                self.last_reconcile_ok = False
                self.scheduler_error = self.scheduler_error or '优先级任务状态暂不可核对'
                return
            if not sid:
                if strict is not None and row['origin'] == 'platform' and not top_marker:
                    # A Slurm job name is user-controlled and cannot establish
                    # ownership of an uncertain submission. Preserve the
                    # original intent until controller-side identity proof exists.
                    unresolved = True
                    self.scheduler_error = self.scheduler_error or '任务提交身份未确认，等待控制器核对'
                    continue
                controller_recovered = False
                if top_marker:
                    try:
                        candidates = [strict.recover_submission_identity(row['owner_uid'], row['id'])]
                        controller_recovered = True
                    except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError, ValueError):
                        top_unknown(row, None, 'priority_binding_unknown', '优先任务调度身份未确认')
                        continue
                else:
                    candidates = [key for key, value in records.items() if value["name"] == "p6-" + row["id"]]
                if not candidates:
                    candidates = [key for key, value in complete.items() if value.get("name") == "p6-" + row["id"]]
                if len(candidates) == 1:
                    sid = candidates[0]
                    if top_marker and not controller_recovered and not self._held_top_record_matches(row, records.get(sid)):
                        top_unknown(row, None, 'priority_binding_unknown', '优先任务调度身份未确认')
                        continue
                    try:
                        changed = self._reconcile_set_scheduler_id(row, sid, top_marker, strict)
                    except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError):
                        unresolved = True
                        self.scheduler_error = self.scheduler_error or '任务调度身份写入失败，等待核对'
                        continue
                    if not changed:
                        if strict is not None and row['origin'] == 'platform':
                            unresolved = True
                            self.scheduler_error = self.scheduler_error or '优先任务状态并发变化，等待核对'
                        continue
                else:
                    if top_marker:
                        top_unknown(row, None, 'priority_binding_unknown', '优先任务调度身份未确认')
                    elif strict is not None and row['origin'] == 'platform' and row['state'] == 'SUBMITTING':
                        unresolved = True
                        self.scheduler_error = self.scheduler_error or '任务提交结果未确认，等待调度器核对'
                    continue  # Ambiguous submit never auto-retries; avoids duplicate jobs.
            if strict is not None and row['origin'] == 'platform' and not top_marker:
                terminal_record = complete.get(sid)
                if not records.get(sid) or records[sid].get('state', '').split('+')[0] in ('COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'NODE_FAIL', 'PREEMPTED', 'BOOT_FAIL', 'DEADLINE', 'OUT_OF_MEMORY'):
                    try:
                        self._reconcile_ordinary_terminal(row, sid, strict, terminal_record)
                    except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError, ValueError):
                        unresolved = True
                        self.scheduler_error = self.scheduler_error or '任务结束身份待核对'
                    continue
                # Only adopt a pending task whose BIND2 identity is already
                # durable on both sides; do not infer identity from scontrol.
                try:
                    with strict.runtime.coordinator.serialized():
                        if not strict.runtime.reconciled_for_current_boot() or strict._has_marker_locked(row['id']):
                            raise ControllerUnavailable('ordinary priority classification unavailable')
                        strict._same_controller()
                        before = strict.runtime.coordinator.transport.status()
                        held = self.store.db.execute(
                            'SELECT uid,scheduler_id,state,binding_version,bind_submit,bind_sluid '
                            'FROM monthly_dispatch_holds WHERE job_id=?', (row['id'],)).fetchone()
                        status = strict.reservation_status_for_job(row['owner_uid'], row['id'])
                        observed = records.get(sid)
                        observed_state = observed.get('state', '').split('+')[0] if observed else ''
                        if (held is not None and held['binding_version'] == 1 and
                                held['uid'] == row['owner_uid'] and held['scheduler_id'] == int(sid) and
                                held['state'] == 'BOUND' and held['bind_submit'] is None and held['bind_sluid'] is None and
                                status.get('scheduler_id') == int(sid) and
                                ((observed_state == 'PENDING' and status.get('state') == 'bound') or
                                 (observed_state == 'RUNNING' and status.get('state') == 'active')) and
                                observed.get('fields', {}).get('UserId', '').endswith('(%d)' % row['owner_uid']) and
                                strict.runtime.coordinator.transport.status() == before and
                                strict.runtime.reconciled_for_current_boot() and
                                strict._verify_effective_job(row['owner_uid'], row['id'], row['effective_job_digest'])):
                            # Preserve the old binding while reflecting its verified live state.
                            db = self.store.db
                            db.execute('BEGIN IMMEDIATE')
                            try:
                                current = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
                                if current is None or dict(current) != dict(row):
                                    raise ControllerUnavailable('legacy task changed')
                                now = utc_now()
                                if db.execute("UPDATE jobs SET state=?,reason_code=?,reason_text=?,updated_at=?,"
                                              "started_at=COALESCE(started_at,?) WHERE id=? AND scheduler_id=?",
                                              (observed_state, 'legacy_scheduler_state', observed_state, now,
                                               now if observed_state == 'RUNNING' else None, row['id'], sid)).rowcount != 1:
                                    raise ControllerUnavailable('legacy task changed')
                                db.execute('COMMIT')
                            except BaseException:
                                if db.in_transaction:
                                    db.execute('ROLLBACK')
                                raise
                            continue
                        receipt = strict.bridge.binding_receipt(row['owner_uid'], row['id'], int(sid))
                        if (held is None or held['uid'] != row['owner_uid'] or held['scheduler_id'] != int(sid) or
                                held['state'] != 'BOUND' or held['binding_version'] != 2 or
                                held['bind_submit'] != str(receipt['submit']) or held['bind_sluid'] != str(receipt['sluid']) or
                                status.get('state') not in ('bound', 'active') or status.get('scheduler_id') != int(sid) or
                                strict.runtime.coordinator.transport.status() != before or
                                not strict.runtime.reconciled_for_current_boot() or
                                not strict._verify_effective_job(row['owner_uid'], row['id'], row['effective_job_digest'])):
                            raise ControllerUnavailable('ordinary binding not confirmed')
                        observed = records.get(sid)
                        observed_state = observed.get('state', '').split('+')[0] if observed else ''
                        if observed_state == 'PENDING' and status.get('state') == 'bound':
                            pass
                        elif observed_state == 'RUNNING' and status.get('state') == 'active':
                            running = strict.bridge.ordinary_running_receipt(row['owner_uid'], row['id'], int(sid))
                            if (running['submit'] != receipt['submit'] or running['sluid'] != receipt['sluid'] or
                                    running['generation'] != status.get('generation') or
                                    running['generation'] > 2**63-1 or
                                    strict.runtime.coordinator.transport.status() != before):
                                raise ControllerUnavailable('ordinary running identity differs')
                            self.store.db.execute('CREATE TABLE IF NOT EXISTS ordinary_running_receipts '
                                                  '(job_id TEXT PRIMARY KEY, uid INTEGER NOT NULL, scheduler_id INTEGER NOT NULL, '
                                                  'submit_time INTEGER NOT NULL, sluid TEXT NOT NULL, generation INTEGER NOT NULL)')
                            expected_running = (row['id'], row['owner_uid'], int(sid), running['submit'], str(running['sluid']), running['generation'])
                            old_running = self.store.db.execute('SELECT * FROM ordinary_running_receipts WHERE job_id=?', (row['id'],)).fetchone()
                            if old_running is None:
                                self.store.db.execute('INSERT INTO ordinary_running_receipts VALUES(?,?,?,?,?,?)', expected_running)
                            elif tuple(old_running) != expected_running:
                                raise ControllerUnavailable('ordinary running receipt conflicts')
                        else:
                            raise ControllerUnavailable('ordinary job state not confirmed')
                except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError, ValueError):
                    unresolved = True
                    self.scheduler_error = self.scheduler_error or '任务调度实例未确认，等待控制器核对'
                    continue
            external = row["origin"] == "external"
            rec = records.get(sid) if external else (records.get(sid) or complete.get(sid))
            expected_name = row["source_scheduler_name"] if external else "p6-" + row["id"]
            if rec and (rec.get("name") != expected_name or (external and rec.get("fields", {}).get("SubmitTime") != row["source_submit_time"])):
                rec = None  # Job IDs can be reused; never mark another job's completion.

            if not rec:
                if top_marker:
                    try:
                        strict.reconcile_terminal_receipt(row['owner_uid'], row['id'], int(sid))
                    except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError, ValueError):
                        top_unknown(row, sid, 'completion_unknown', '优先任务调度状态未确认')
                else:
                    try:
                        if not self._reconcile_missing_ordinary(row, strict):
                            unresolved = True
                            self.scheduler_error = self.scheduler_error or '任务状态并发变化，等待核对'
                    except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError):
                        unresolved = True
                        self.scheduler_error = self.scheduler_error or '优先级任务状态暂不可核对'
                continue
            state = rec["state"].split("+")[0]
            if not state:
                if top_marker:
                    top_unknown(row, sid, 'priority_binding_unknown', '优先任务调度状态未确认')
                continue
            if top_marker and state == 'RUNNING':
                try:
                    strict.reconcile_running_generation(row['owner_uid'], row['id'], int(sid))
                except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError, ValueError):
                    unresolved = True
                    self.scheduler_error = self.scheduler_error or '优先任务运行身份待核对'
                continue
            if top_marker and state in ('COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'NODE_FAIL', 'PREEMPTED', 'BOOT_FAIL', 'DEADLINE', 'OUT_OF_MEMORY'):
                try:
                    strict.reconcile_terminal_receipt(row['owner_uid'], row['id'], int(sid))
                except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError, ValueError):
                    unresolved = True
                    self.scheduler_error = self.scheduler_error or '优先任务结束身份待核对'
                continue
            if top_marker and state != 'PENDING':
                # Running/terminal identity and marker cleanup are not yet
                # reconciled by the experimental path. Do not alter the job.
                unresolved = True
                self.scheduler_error = self.scheduler_error or '优先任务运行或结束状态待核对'
                continue
            if top_marker and state == 'PENDING' and not self._held_top_record_matches(row, rec):
                # Unheld jobs are accepted only through the controller's exact
                # read-only BIND2+BIND5 receipt below, not from this display.
                fields = rec.get('fields', {})
                user = fields.get('UserId', '')
                match = re.fullmatch(r'.*\(([0-9]+)\)', user)
                uid_text = match.group(1) if match else user
                if (rec.get('name') != 'p6-' + row['id'] or not uid_text.isdigit() or
                        int(uid_text) != row['owner_uid'] or
                        fields.get('Comment') != 'pro6000d-job:' + row['id'] + ':tier=' + str(row['selected_tier'])):
                    top_unknown(row, sid, 'priority_binding_unknown', '优先任务身份未确认')
                    continue
            final = state not in ("PENDING", "RUNNING", "COMPLETING", "SUSPENDED", "CONFIGURING", "RESIZING", "REQUEUED", "REQUEUE_FED")
            if row["selected_tier"] is not None:
                try:
                    self._reconcile_monthly_reservation(row, sid, final, top_marker, strict)
                except (ReservationUncertain, ControllerUnavailable, OSError, sqlite3.Error, RuntimeError):
                    if strict is not None and row['origin'] == 'platform':
                        # The row or marker may have changed while the lock or
                        # controller was unavailable. Do not write through a
                        # stale scheduler observation; keep dispatch closed.
                        unresolved = True
                        self.scheduler_error = self.scheduler_error or '月度预留绑定未确认，等待核对'
                    elif top_marker:
                        top_unknown(row, sid, 'binding_uncertain', '月度预留绑定未确认，等待核对')
                    else:
                        self.store.db.execute("UPDATE jobs SET reason_code='binding_uncertain',reason_text='月度预留绑定未确认，等待核对',updated_at=? WHERE id=?", (utc_now(), row["id"]))
                    continue
            if top_marker and state == 'PENDING':
                try:
                    receipt = strict.bind_held(row['owner_uid'], row['owner_name'], row['id'], int(sid),
                                               readonly=not self._held_top_record_matches(row, rec))
                    changed = self._reconcile_set_top_held(row, sid, receipt, strict)
                except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError):
                    changed = False
                if not changed:
                    unresolved = True
                    self.scheduler_error = self.scheduler_error or '优先任务绑定待核对，保持挂起'
                    continue
                continue
            reason = rec.get("reason", "")
            if external:
                reason = ("外部任务 GPU 数量未知，按整机保守预留；" if row["resource_unknown"] else "外部 Slurm 任务，仅查看；") + (reason or state)
            if state in ("CONFIGURING", "RESIZING", "REQUEUED", "REQUEUE_FED"):
                state = "PENDING"
            try:
                changed = self._reconcile_set_ordinary_state(row, sid, state, reason, final, strict)
            except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError):
                changed = False
            if not changed:
                unresolved = True
                self.scheduler_error = self.scheduler_error or '任务状态并发变化或写入失败，等待核对'
                continue
            if final and row["state"] != state:
                self.add_notification(row["owner_uid"], "job_finished", "任务已结束", "%s：%s" % (row["name"], state))
        strict = getattr(self, 'strict_admission', None)
        # Binding writes re-fence the controller. Re-audit on every successful
        # reconciliation, outside the coordinator scopes used above.
        if not unresolved and strict is not None and strict.runtime.cutover_ready:
            try:
                strict._same_controller()
                if not strict.bridge.supports_top_uncertainty_clear():
                    raise ControllerUnavailable('controller clear capability unavailable')
                strict.clear_uncertainty_after_reconciliation()
            except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError):
                unresolved = True
                self.scheduler_error = self.scheduler_error or '优先级屏障状态待核对'
        self.last_reconcile_ok = not unresolved and (strict is None or strict.runtime.cutover_ready)

    def completion_records(self) -> Dict[str, Any]:
        records = {}
        path = self.config.get("jobcomp_log", "/var/log/slurm/jobcomp.log")
        try:
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 8 * 1024 * 1024))
                content = fh.read(8 * 1024 * 1024).decode("utf-8", "replace")
            for line in content.splitlines():
                fields = dict(re.findall(r"(?:^| )([A-Za-z][A-Za-z0-9_]*)=([^ ]+)", line))
                sid = fields.get("JobId", "")
                if sid.isdigit():
                    records[sid] = {"name": fields.get("Name", fields.get("JobName", "")), "state": fields.get("JobState", fields.get("State", "")), "reason": fields.get("ExitCode", "")}
        except OSError:
            pass
        return records

    def tick(self) -> None:
        try:
            self._tick_once()
        except Exception:
            if getattr(self, 'strict_admission', None) is not None:
                self.last_reconcile_ok = False
                self.scheduler_error = '调度核对未完成，等待重试'
            raise

    def _tick_once(self) -> None:
        if time.monotonic() - self.last_tick < 3:
            return
        self.last_tick = time.monotonic()
        self.reconcile()
        self.monthly.refresh()
        strict = getattr(self, 'strict_admission', None)
        if strict is not None:
            try:
                # Finish a persisted promotion before expiry tries to cancel
                # its protected members. Claim still validates approval expiry.
                strict.recover_pending()
            except Exception as exc:
                # submit() also invokes dispatch_waiting directly after tick.
                # Preserve the failed recovery gate for every such entry point.
                self.last_reconcile_ok = False
                print('priority marker recovery unavailable: %s' % exc, file=sys.stderr, flush=True)
                # Persist expiry requests even while the controller is down;
                # the promotion remains fenced until tombstones are confirmed.
                try:
                    self.expire_applications()
                except Exception as expiry_exc:
                    print('expiry recording unavailable: %s' % expiry_exc, file=sys.stderr, flush=True)
                return
        self.expire_applications()
        self.reconcile_reservation_cancellations()
        if self.last_reconcile_ok:
            self.dispatch_waiting()

    def gpu_xml(self) -> bytes:
        """One fixed read-only probe, bounded in wall time and captured bytes."""
        executable = shutil.which("nvidia-smi")
        if not executable:
            raise ValueError("nvidia-smi 未安装")
        process = subprocess.Popen([executable, "-q", "-x"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env={"PATH":"/usr/bin:/bin", "LANG":"C.UTF-8"})
        selector = selectors.DefaultSelector()
        output = bytearray(); errors = bytearray(); deadline = time.monotonic() + 3
        try:
            selector.register(process.stdout, selectors.EVENT_READ, output)
            selector.register(process.stderr, selectors.EVENT_READ, errors)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("GPU 采样超时（3 秒）")
                for key, _ in selector.select(remaining):
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    key.data.extend(block)
                    limit = 2097152 if key.data is output else 8192
                    if len(key.data) > limit:
                        raise ValueError("GPU 采样输出超过限制")
            process.wait(timeout=max(0.01, deadline-time.monotonic()))
            if process.returncode:
                raise ValueError(errors.decode("utf-8", "replace").strip()[:400] or "NVIDIA 驱动不可用")
            return bytes(output)
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close(); process.stderr.close()

    @staticmethod
    def gpu_number(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        match = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*(?:[A-Za-z%/]+)?\s*$", value)
        return float(match.group(1)) if match else None

    @staticmethod
    def process_snapshot(pid: int) -> Tuple[int, str]:
        base = pathlib.Path("/proc") / str(pid)
        uid = base.stat().st_uid
        with (base / "stat").open() as stream:
            value = stream.read(8192)
        tail = value[value.rfind(")") + 2:].split()
        return uid, tail[19]

    def gpu_slurm_pid_map(self, pids: Iterable[int]) -> Dict[int, Any]:
        """One bounded lookup per sample; bind listpids evidence to a process lifetime."""
        before = {}
        for pid in set(pids):
            try:
                before[pid] = self.process_snapshot(pid)
            except (OSError, ValueError, IndexError):
                pass
        if not before:
            return {}
        rows = self.store.db.execute("SELECT scheduler_id,owner_uid FROM jobs WHERE state IN ('RUNNING','COMPLETING','SUSPENDED') AND scheduler_id IS NOT NULL ORDER BY created_at DESC LIMIT 32").fetchall()
        deadline = time.monotonic() + 2.0
        candidates = {}
        ambiguous = set()
        for row in rows:
            sid = row["scheduler_id"]
            if not sid.isdigit() or time.monotonic() >= deadline:
                continue
            if not any(identity[0] == row["owner_uid"] for identity in before.values()):
                continue
            env = {"PATH":"/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin", "LANG":"C.UTF-8"}
            if self.config.get("slurm_conf"):
                env["SLURM_CONF"] = self.config["slurm_conf"]
            proc = None
            selector = selectors.DefaultSelector()
            data = bytearray()
            complete = False
            try:
                proc = subprocess.Popen(["scontrol", "listpids", sid], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
                selector.register(proc.stdout, selectors.EVENT_READ)
                while time.monotonic() < deadline:
                    events = selector.select(max(0, deadline-time.monotonic()))
                    if not events:
                        break
                    chunk = os.read(proc.stdout.fileno(), 16384)
                    if not chunk:
                        complete = proc.wait(timeout=max(0.001,deadline-time.monotonic())) == 0
                        break
                    data.extend(chunk)
                    if len(data) > 256 * 1024:
                        break
                if complete:
                    for line in data.decode("utf-8", "replace").splitlines():
                        fields = line.split()
                        if len(fields) < 2 or not fields[0].isdigit() or fields[1] != sid:
                            continue
                        pid = int(fields[0])
                        identity = before.get(pid)
                        if identity is None or identity[0] != row["owner_uid"]:
                            continue
                        entry = (sid, identity)
                        if pid in candidates and candidates[pid] != entry:
                            ambiguous.add(pid)
                        candidates[pid] = entry
            except (OSError, subprocess.TimeoutExpired):
                pass
            finally:
                selector.close()
                if proc is not None:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait()
                    proc.stdout.close()
        verified = {}
        for pid, entry in candidates.items():
            try:
                if pid not in ambiguous and self.process_snapshot(pid) == entry[1]:
                    verified[pid] = entry
            except (OSError, ValueError, IndexError):
                pass
        return verified

    def gpu_process_identity(self, pid: int, slurm_pids: Optional[Dict[int, Any]] = None) -> Dict[str, Any]:
        """Never read cmdline/environment; verify UID and process start time twice."""
        base = pathlib.Path("/proc") / str(pid)
        unknown = {"uid":None,"username":None,"name":"未知进程","job_id":None,
                   "job_name":None,"scheduler_id":None,"association":"unavailable"}
        try:
            before = self.process_snapshot(pid)
            try:
                name = os.path.basename(os.readlink(str(base / "exe")))[:200]
            except OSError:
                with (base / "comm").open() as stream:
                    name = stream.read(256).strip()[:200]
            with (base / "cgroup").open() as stream:
                groups = stream.read(32768)
            if self.process_snapshot(pid) != before:
                return unknown
            uid = before[0]
            try:
                username = pwd.getpwuid(uid).pw_name
            except KeyError:
                username = str(uid)
            result = {**unknown,"uid":uid,"username":username,"name":name or "未知进程","association":"unassociated"}
            job_ids = set()
            for line in groups.splitlines():
                path = line.split(":",2)[-1]
                job_ids.update(re.findall(r"(?:^|/)job_(\d+)(?:/|$)",path))
            if not job_ids and slurm_pids and pid in slurm_pids:
                sid, lifetime = slurm_pids[pid]
                if lifetime == before:
                    job_ids.add(sid)
            if len(job_ids) == 1:
                sid = next(iter(job_ids))
                row = self.store.db.execute("SELECT id,name,scheduler_id FROM jobs WHERE scheduler_id=? AND owner_uid=? ORDER BY created_at DESC LIMIT 1",(sid,uid)).fetchone()
                if row:
                    result.update(job_id=row["id"],job_name=row["name"],scheduler_id=row["scheduler_id"],association="matched")
            return result if self.process_snapshot(pid) == before else unknown
        except (OSError, ValueError, IndexError):
            return unknown

    def sample_gpu(self) -> None:
        now = time.monotonic()
        if self.gpu_sample_monotonic is not None and now-self.gpu_sample_monotonic < 5:
            return
        gap = self.gpu_sample_monotonic is not None and now-self.gpu_sample_monotonic > 15
        self.gpu_sample_monotonic = now
        attempted = utc_now()
        if gap:
            self.gpu_history.append({"observed_at":attempted,"available":False,"devices":[],"reason":"sampling_gap"})
        try:
            raw = self.gpu_xml()
            if b"<!ENTITY" in raw:
                raise ValueError("GPU XML 包含不支持的实体定义")
            root = ET.fromstring(raw)
            driver = root.findtext("driver_version")
            if driver in ("", "N/A", "[Not Supported]"):
                driver = None
            devices = []; processes = []; truncated = False
            gpu_nodes = root.findall("gpu")
            if not gpu_nodes:
                raise ValueError("未检测到 NVIDIA GPU")
            identities = {}
            gpu_pids = []
            for node in gpu_nodes[:16]:
                for proc in node.findall("processes/process_info"):
                    value = proc.findtext("pid") or ""
                    if value.isdigit() and int(value) > 0:
                        gpu_pids.append(int(value))
                    if len(gpu_pids) >= 512:
                        break
                if len(gpu_pids) >= 512:
                    break
            slurm_pids = self.gpu_slurm_pid_map(gpu_pids)
            for index, node in enumerate(gpu_nodes[:16]):
                def number(*paths):
                    for path in paths:
                        value = self.gpu_number(node.findtext(path))
                        if value is not None: return value
                    return None
                gpu_id = node.findtext("uuid") or node.get("id") or str(index)
                device = {"index":str(index),"id":gpu_id,"name":node.findtext("product_name") or "NVIDIA GPU",
                          "memory_total_mb":number("fb_memory_usage/total"),"memory_used_mb":number("fb_memory_usage/used"),
                          "utilization_percent":number("utilization/gpu_util"),"owner_job_id":None,
                          "temp_c":number("temperature/gpu_temp"),"fan_percent":number("fan_speed"),
                          "power_w":number("gpu_power_readings/instant_power_draw","gpu_power_readings/average_power_draw","power_readings/power_draw"),
                          "power_limit_w":number("gpu_power_readings/current_power_limit","power_readings/power_limit"),
                          "graphics_clock_mhz":number("clocks/graphics_clock"),"memory_clock_mhz":number("clocks/mem_clock"),
                          "memory_utilization_percent":number("utilization/memory_util"),
                          "pstate":node.findtext("performance_state") if node.findtext("performance_state") not in ("", "N/A", "[Not Supported]") else None,
                          "pcie_generation":number("pci/pci_gpu_link_info/pcie_gen/current_link_gen"),
                          "pcie_width":number("pci/pci_gpu_link_info/link_widths/current_link_width"),
                          "encoder_utilization_percent":number("utilization/encoder_util"),
                          "decoder_utilization_percent":number("utilization/decoder_util"),"driver_version":driver}
                devices.append(device)
                for proc in node.findall("processes/process_info"):
                    if len(processes) >= 512:
                        truncated = True; break
                    try: pid = int(proc.findtext("pid") or "")
                    except ValueError: continue
                    if pid <= 0: continue
                    if pid not in identities: identities[pid] = self.gpu_process_identity(pid, slurm_pids)
                    kind = (proc.findtext("type") or "").strip()
                    processes.append({**identities[pid],"gpu_uuid":gpu_id,"gpu_index":str(index),"pid":pid,
                                      "type":kind if kind in ("C","G","C+G","M") else "unknown",
                                      "memory_used_mb":self.gpu_number(proc.findtext("used_memory")),"utilization_percent":None})
            observed = utc_now()
            self.gpu_cache = {"available":True,"stale":False,"status":"ok","reason":None,"devices":devices,
                              "processes":processes,"driver_version":driver,"observed_at":observed,
                              "last_attempt_at":attempted,"processes_truncated":truncated,
                              "devices_truncated":len(gpu_nodes)>16,"ownership":"unknown_without_slurm_gres_mapping"}
            self.gpu_history.append({"observed_at":observed,"available":True,"gap_before":gap,
                                     "devices":[{k:d[k] for k in ("id","index","utilization_percent","memory_used_mb","memory_total_mb")} for d in devices]})
        except (OSError, ValueError, ET.ParseError, subprocess.TimeoutExpired) as exc:
            previous = self.gpu_cache or {"devices":[],"processes":[],"observed_at":None,"driver_version":None}
            self.gpu_cache = {**previous,"available":False,"stale":bool(previous.get("devices")),
                              "status":"stale" if previous.get("devices") else "unavailable",
                              "reason":str(exc)[:400],"last_attempt_at":attempted}
            self.gpu_history.append({"observed_at":attempted,"available":False,"devices":[],"reason":"sampling_failed"})

    def gpu_inventory(self, ident: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.sample_gpu()
        cached = self.gpu_cache or {"available":False,"devices":[],"processes":[],"observed_at":None}
        processes = cached.get("processes", [])
        visible = processes if ident and ident["is_admin"] else [p for p in processes if ident and p["uid"] == ident["uid"]]
        observed = parse_iso(cached.get("observed_at"))
        return {**cached,"processes":visible,"hidden_process_count":len(processes)-len(visible),
                "process_visibility":"all" if ident and ident["is_admin"] else "own_only",
                "history":list(self.gpu_history),"history_started_at":self.gpu_history[0]["observed_at"] if self.gpu_history else None,
                "service_started_at":self.service_started_at,"history_scope":"since_service_start",
                "sampling_interval_seconds":5,"age_seconds":max(0,int((dt.datetime.now(dt.timezone.utc)-observed).total_seconds())) if observed else None}

    def metrics(self, ident: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        mem_total = mem_available = None
        try:
            data = pathlib.Path("/proc/meminfo").read_text().splitlines()
            vals = {line.split(":", 1)[0]: int(line.split()[1]) for line in data if ":" in line}
            mem_total, mem_available = vals.get("MemTotal", 0) // 1024, vals.get("MemAvailable", 0) // 1024
        except (OSError, ValueError, IndexError):
            pass
        cpu_load = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
        return {"available": True, "observed_at": utc_now(), "cpu_load_1m": cpu_load,
                "memory_total_mb": mem_total, "memory_available_mb": mem_available,
                "gpu": self.gpu_inventory(ident), "scheduler": {"available": self.scheduler_available()}}

    # ---------- jobs ----------
    def row_job(self, row: sqlite3.Row) -> Dict[str, Any]:
        result = {"name": row["name"], "qos": row["qos"], "gpu": row["requested_gpu"],
                  "cpu": row["requested_cpu"], "memory_mb": row["requested_mem_mb"],
                  "time_minutes": row["requested_minutes"], "workdir": row["workdir"],
                  "environment": row["env_name"], "command": row["command"], "output_path": row["output_path"]}
        if "selected_tier" in row.keys() and row["selected_tier"] is not None:
            result["selected_tier"] = row["selected_tier"]
        return result

    @staticmethod
    def effective_job_digest(jid: str, uid: int, job: Dict[str, Any], app_id: Optional[str]) -> str:
        """Hash validated fields only; this is not an allocation authorization."""
        effective = {"version": 1, "job_id": jid, "uid": uid,
                     **{key: job[key] for key in ("name", "gpu", "cpu", "memory_mb", "time_minutes", "qos", "workdir", "environment", "command", "output_path")},
                     "selected_tier": job.get("selected_tier"), "temporary_application_id": app_id}
        return hashlib.sha256(json.dumps(effective, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()

    def monthly_controller_snapshot(self, uid: int) -> Dict[str, Any]:
        cfg = self.config.get("monthly_controller", {})
        if not isinstance(cfg, dict) or cfg.get("read_enabled") is not True:
            if isinstance(cfg, dict) and cfg.get("activation_enabled") is True:
                return unavailable(True)
            return self.monthly.summary(uid)
        try:
            controller_uid = pwd.getpwnam(cfg.get("controller_user", "slurm")).pw_uid
            bridge = ControllerBridge(cfg.get("socket_path", "/var/lib/slurm/ctld/time-quota.sock"), controller_uid)
            verified_pid = None
            if cfg.get("activation_enabled") is True:
                verified_pid = verify_release(cfg.get("release_manifest", "/etc/pro6000d-training/monthly-release.json"),
                                              controller_uid, return_controller_pid=True)
                if verified_pid:
                    bridge.expected_pid = verified_pid
            snapshot = bridge.selectable_snapshot(uid)
            observed = snapshot.get("observed_sec")
            if (snapshot.get("month") != int(month_now().replace("-", "")) or
                    isinstance(observed, bool) or not isinstance(observed, int) or
                    abs(time.time() - observed) > 15):
                raise ValueError("stale controller snapshot")
            # The deployer enables this only after paired binaries and
            # configuration are verified; the live controller must also
            # attest the recovered, enabled protocol on every read.
            verified = bool(verified_pid) and bridge.readiness() is True
            view = selectable_snapshot_view(snapshot, verified)
            view['request_available'] = verified and cfg.get('grant_enabled') is True
            return view
        except (KeyError, TypeError, ValueError, ControllerUnavailable):
            return unavailable(False)

    def verified_monthly_bridge(self) -> ControllerBridge:
        cfg = self.config.get("monthly_controller", {})
        if not isinstance(cfg, dict) or cfg.get("activation_enabled") is not True or cfg.get("read_enabled") is not True:
            raise ControllerUnavailable("monthly release is not enabled")
        try:
            uid = pwd.getpwnam(cfg.get("controller_user", "slurm")).pw_uid
            manifest = cfg.get("release_manifest", "/etc/pro6000d-training/monthly-release.json")
            pid = verify_release(manifest,
                                 uid, return_controller_pid=True)
            if not pid:
                raise ControllerUnavailable("monthly release artifacts are not verified")
            bridge = ControllerBridge(cfg.get("socket_path", "/var/lib/slurm/ctld/time-quota.sock"), uid,
                                      release_manifest=manifest)
            bridge.expected_pid = pid
            if not bridge.readiness():
                raise ControllerUnavailable("monthly controller is not ready")
            return bridge
        except (KeyError, TypeError, ValueError) as exc:
            raise ControllerUnavailable("controller configuration unavailable") from exc

    def monthly_reservation_coordinator(self) -> ReservationCoordinator:
        try:
            return ReservationCoordinator(self.store.db, self.verified_monthly_bridge())
        except ControllerUnavailable as exc:
            raise ReservationUncertain("controller configuration unavailable") from exc

    def _strict_monthly_hold(self, row, selected, job, strict, actual_bridge=None):
        """Reserve under the priority lock against the same attested controller."""
        db = self.store.db
        with strict.runtime.coordinator.serialized():
            def reserve(policy, status):
                current = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
                if (current is None or any(current[k] != row[k] for k in current.keys()) or
                        current['state'] != 'WAITING_QUOTA' or current['scheduler_id'] is not None or
                        strict._has_marker_locked(row['id']) is not policy['enabled'] or
                        not strict._verify_effective_job(row['owner_uid'], row['id'], row['effective_job_digest'])):
                    raise ReservationUncertain('task or priority changed before reservation')
                strict._same_controller()
                source = self.verified_monthly_bridge()
                peer = strict.bridge
                if (source.path, source.controller_uid, source.expected_pid, source.release_manifest) != (
                        peer.path, peer.controller_uid, peer.expected_pid, peer.release_manifest):
                    raise ReservationUncertain('reservation controller differs from priority controller')
                pinned = actual_bridge or PinnedControllerBridge(source.path, source.controller_uid, source.expected_pid,
                                                source.timeout, release_manifest=source.release_manifest)
                if (pinned.path, pinned.controller_uid, pinned.expected_pid, pinned.release_manifest) != (
                        source.path, source.controller_uid, source.expected_pid, source.release_manifest):
                    raise ReservationUncertain('actual usage controller differs')
                if not pinned.readiness():
                    raise ReservationUncertain('reservation controller unavailable')
                ReservationCoordinator(db, pinned).hold(row['id'], row['owner_uid'], selected, job['gpu'],
                    job['time_minutes'], int(month_now().replace('-', '')), require_waiting=True, policy_version=row['policy_version'])
                def reverify(current_policy, after):
                    policy_fields = ('uid', 'username', 'identity_generation', 'enabled', 'version')
                    if (any(current_policy.get(k) != policy.get(k) for k in policy_fields) or
                            any(after.get(k) != status.get(k) for k in ('epoch', 'boot', 'watermark', 'fenced')) or
                            ('ledger_seq' in status and (type(after.get('ledger_seq')) is not int or
                             after['ledger_seq'] < status['ledger_seq'])) or
                            strict.runtime.coordinator.transport.status() != after or
                            not strict.runtime.reconciled_for_current_boot()):
                        raise ReservationUncertain('controller changed during reservation')
                strict.runtime._with_verified_policy_locked(row['owner_uid'], row['owner_name'], reverify)
                current_after = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
                if (current_after is None or tuple(current_after) != tuple(current) or
                        strict._has_marker_locked(row['id']) is not policy['enabled'] or
                        not strict._verify_effective_job(row['owner_uid'], row['id'], row['effective_job_digest'])):
                    raise ReservationUncertain('task changed during reservation')
            return strict.runtime._with_verified_policy_locked(row['owner_uid'], row['owner_name'], reserve)

    def _dispatch_note_waiting(self, row, code, message):
        """Update a waiting reason without overwriting a concurrent claim."""
        db = self.store.db
        strict = getattr(self, 'strict_admission', None)
        sql = "UPDATE jobs SET reason_code=?,reason_text=?,updated_at=? WHERE id=? AND state='WAITING_QUOTA'"
        params = (code, message, utc_now(), row['id'])
        if strict is None:
            return db.execute(sql, params).rowcount == 1
        with strict.runtime.coordinator.serialized():
            db.execute('BEGIN IMMEDIATE')
            try:
                current = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
                classification = row['priority_marker_required']
                if (current is None or any(current[k] != row[k] for k in current.keys()) or
                        classification not in (0, 1) or
                        strict._has_marker_locked(row['id']) is not bool(classification) or
                        not strict._verify_effective_job(row['owner_uid'], row['id'], row['effective_job_digest'])):
                    db.execute('ROLLBACK')
                    return False
                changed = db.execute(sql, params).rowcount == 1
                db.execute('COMMIT')
                return changed
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def claim_waiting_job(self, row: sqlite3.Row) -> bool:
        """Claim only the exact job fields inspected before submission."""
        fields = ("origin", "scheduler_id", "owner_uid", "owner_name", "client_request_id", "client_request_hash",
                  "priority_marker_required", "name", "requested_gpu", "requested_cpu", "requested_mem_mb",
                  "requested_minutes", "qos", "workdir", "env_name", "command", "output_path",
                  "selected_tier", "temporary_application_id", "effective_job_digest_version",
                  "effective_job_digest", "policy_version", "policy_digest")
        where = " AND ".join(name + " IS ?" for name in fields)
        sql = ("UPDATE jobs SET state='SUBMITTING',reason_code='submitting',"
               "reason_text='正在提交调度器',updated_at=? WHERE id=? AND state='WAITING_QUOTA' AND " + where +
               " AND (temporary_application_id IS NULL OR temporary_application_id='' OR EXISTS ("
               "SELECT 1 FROM applications a WHERE a.id=jobs.temporary_application_id "
               "AND a.owner_uid=jobs.owner_uid AND a.kind='temporary_job' AND a.status='APPROVED' "
               "AND a.policy_version=jobs.policy_version "
               "AND a.authorization_used=1 AND a.bound_job_id=jobs.id "
               "AND (a.expires_at GLOB '*Z' OR a.expires_at GLOB '*[+-][0-9][0-9]:[0-9][0-9]') "
               "AND julianday(a.expires_at)>julianday('now')))" )
        return self.store.db.execute(sql, (utc_now(), row['id'], *(row[name] for name in fields))).rowcount == 1

    def sbatch_argv(self, row, job, user, job_gid, script):
        selected = job.get('selected_tier')
        argv = ["sbatch", "--parsable", "--uid", str(user.pw_uid), "--gid", str(job_gid),
                "--job-name", "p6-" + row["id"], "--partition", self.config.get("partition", "gpu"),
                "--comment", "pro6000d-job:" + row["id"] + ((":tier=" + selected) if selected is not None else ""), "--gres", "gpu:%d" % job["gpu"],
                "--cpus-per-task", str(job["cpu"]), "--mem", "%dM" % job["memory_mb"],
                "--time", "0" if row['policy_version'] == 2 else str(job["time_minutes"]), "--chdir", job["workdir"],
                "--export", "ALL", "--output", job["output_path"], "--error", job["output_path"], str(script)]
        if selected is not None:
            argv[-1:-1] = ["--nodes=1-1"]
        if self.config.get("slurm_account"):
            argv[-1:-1] = ["--account", str(self.config["slurm_account"])]
            if row['policy_version'] == 1:
                argv[-1:-1] = ["--qos", job["qos"]]
        if row['policy_version'] == 2:
            if not isinstance(job['qos'], str) or not SAFE_NAME.fullmatch(job['qos']):
                raise ApiError(409, 'invalid_policy', '任务 QoS 快照无效')
            argv[-1:-1] = ["--qos", job["qos"], "--no-requeue"]
        return argv

    def dispatch_job(self, ident: Dict[str, Any], row: sqlite3.Row, job: Dict[str, Any], temporary: bool = False) -> Dict[str, Any]:
        if row["origin"] != "platform":
            raise ApiError(409, "external_job_read_only", "外部 Slurm 任务不能通过平台重新提交")
        if row['policy_version'] not in (1, 2):
            raise ApiError(409, 'invalid_policy', '任务计费版本无效')
        actual_bridge = None
        if row['policy_version'] == 2:
            if job['time_minutes'] != 4294967295 or not isinstance(job['qos'], str) or not SAFE_NAME.fullmatch(job['qos']) or job.get('selected_tier') is None:
                raise ApiError(409, 'invalid_policy', '任务计费参数不一致')
            try:
                source = self.verified_monthly_bridge()
                actual_bridge = ActualUsageControllerBridge(source.path, source.controller_uid, source.expected_pid,
                                                            source.timeout, release_manifest=source.release_manifest)
                actual_bridge.require_actual_usage_policy()
            except ControllerUnavailable as exc:
                raise ApiError(503, 'actual_usage_not_ready', '按实际运行计费尚未就绪，任务继续等待') from exc
        version, digest = row["effective_job_digest_version"], row["effective_job_digest"]
        if version is not None or digest is not None:
            try:
                expected = self.effective_job_digest(row["id"], row["owner_uid"], job, row["temporary_application_id"])
                if version != 1 or not isinstance(digest, str) or not hmac.compare_digest(digest, expected):
                    raise ValueError("effective job snapshot mismatch")
                if row['policy_version'] == 2 and row['policy_digest'] != hashlib.sha256(('monthly-policy:2:' + digest).encode('ascii')).hexdigest():
                    raise ValueError("job policy snapshot mismatch")
            except (KeyError, TypeError, ValueError) as exc:
                raise ApiError(409, "job_snapshot_mismatch", "任务内容与入队记录不一致，请重新提交") from exc
        selected = job.get("selected_tier")
        if getattr(self, 'strict_admission', None) is not None and selected is None:
            raise ApiError(409, 'selected_tier_required', '优先级控制要求任务选择月度额度档位')
        coordinator = None
        strict = getattr(self, 'strict_admission', None)
        cfg = self.config.get("monthly_controller", {})
        configured = isinstance(cfg, dict) and cfg.get("activation_enabled") is True
        if configured and selected is None:
            raise ApiError(409, "selected_tier_required", "原有排队任务需要选择月度额度档位")
        if strict is not None and row['priority_marker_required'] == 0:
            try:
                # Avoid reserving ordinary monthly capacity while a verified
                # top-priority task already waits. The real claim repeats this
                # check after the reservation, under the same coordinator lock.
                if strict.claim_for_dispatch(row['owner_uid'], row['owner_name'], row['id'], digest, lambda: True) is not False:
                    raise ControllerUnavailable('ordinary task priority classification changed')
            except PriorityBarrierWait as exc:
                raise ApiError(409, 'priority_wait', '最高优先级任务正在等待，任务继续排队') from exc
            except Exception as exc:
                self.last_reconcile_ok = False
                self.scheduler_error = '优先级屏障未确认，等待核对'
                raise ApiError(409, 'priority_wait', '优先级屏障尚未核对，任务继续等待') from exc
        if selected is not None:
            try:
                readiness = self.monthly_controller_snapshot(row["owner_uid"])
            except Exception as exc:
                if strict is None:
                    raise
                self.last_reconcile_ok = False
                self.scheduler_error = '月度控制器未就绪，等待核对'
                raise ApiError(503, 'monthly_gate_unavailable', '月度控制器尚未核对，任务继续等待') from exc
            if readiness.get("enforcement_ready") is not True or readiness.get("selectable_tiers_ready") is not True:
                if strict is not None:
                    self.last_reconcile_ok = False
                    self.scheduler_error = '月度控制器未就绪，等待核对'
                raise ApiError(409, "selected_tier_unavailable", "月度选档控制尚未就绪")
            try:
                if strict is not None:
                    self._strict_monthly_hold(row, selected, job, strict, actual_bridge)
                else:
                    coordinator = ReservationCoordinator(self.store.db, actual_bridge) if actual_bridge else self.monthly_reservation_coordinator()
                    coordinator.hold(row["id"], row["owner_uid"], selected, job["gpu"], job["time_minutes"],
                                     int(month_now().replace("-", "")), require_waiting=True, policy_version=row['policy_version'])
            except ReservationDeferred as exc:
                raise ApiError(409, "monthly_capacity_wait", "所选档位余额不足以覆盖申请时限；任务继续排队") from exc
            except Exception as exc:
                if strict is not None:
                    self.last_reconcile_ok = False
                    self.scheduler_error = '月度预留未确认，等待核对'
                elif not isinstance(exc, (ReservationUncertain, ControllerUnavailable, OSError, sqlite3.Error, RuntimeError)):
                    raise
                raise ApiError(503, "reservation_unconfirmed", "月度预留未确认；任务保持排队") from exc
        user = self.user(row["owner_uid"])
        job_gid = grp.getgrnam(self.config.get("job_group", "pro6000d-gpu")).gr_gid
        script_dir = pathlib.Path(self.config.get("job_script_dir", "/var/lib/pro6000d-training/jobs"))
        script_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        script = script_dir / (row["id"] + ".sh")
        expected = self.render_script(row["id"], user, job)
        try:
            fd = os.open(str(script), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            st=script.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o077 or script.read_text() != expected:
                raise ApiError(409,"job_script_conflict","已有任务脚本不符合当前任务")
        else:
            with os.fdopen(fd, "w") as fh:
                fh.write(expected)
                fh.flush(); os.fsync(fh.fileno())
        # Persist the intent before submission. In strict mode an uncertain
        # result remains SUBMITTING until controller identity can be proven.
        if row['policy_version'] == 2:
            try:
                actual_bridge.require_actual_usage_policy()
            except ControllerUnavailable as exc:
                raise ApiError(503, 'actual_usage_not_ready', '按实际运行计费尚未就绪，任务继续等待') from exc
        top_held = False
        submission_snapshot = []
        if strict is not None:
            def claim_and_capture():
                db = self.store.db
                db.execute('BEGIN IMMEDIATE')
                try:
                    if not self.claim_waiting_job(row):
                        db.execute('ROLLBACK')
                        return False
                    current = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
                    if current is None:
                        raise ControllerUnavailable('claimed task disappeared')
                    db.execute('COMMIT')
                    submission_snapshot.append(current)
                    return True
                except BaseException:
                    if db.in_transaction:
                        db.execute('ROLLBACK')
                    raise
            try:
                top_held = strict.claim_for_dispatch(row['owner_uid'], row['owner_name'], row['id'], digest,
                                                     claim_and_capture)
                if len(submission_snapshot) != 1:
                    raise ControllerUnavailable('submission claim was not captured')
            except PriorityBarrierWait as exc:
                raise ApiError(409, 'priority_wait', '最高优先级任务正在等待，任务继续排队') from exc
            except Exception as exc:
                self.last_reconcile_ok = False
                self.scheduler_error = '任务提交身份未确认，等待核对'
                raise ApiError(409, 'priority_wait', '优先级屏障或任务登记尚未核对，任务继续等待') from exc
        elif not self.claim_waiting_job(row):
            raise ApiError(409, "submission_already_claimed", "任务状态已变化，等待核对")
        # ALL exports only run()'s explicit sanitized environment. On Slurm 19,
        # NONE implicitly asks Slurm to collect a login environment through PAM.
        argv = self.sbatch_argv(row, job, user, job_gid, script)
        no_hold = False
        if top_held:
            attempted = [False]
            def submit_unheld():
                return run_spawn_locked(argv, timeout=20, attempted=attempted)
            try:
                if not isinstance(cfg, dict) or cfg.get('experimental_creation_bound_top') is not True:
                    raise ControllerUnavailable('creation-bound submission disabled')
                code, out, err = strict.submit_creation_bound_top(
                    row['owner_uid'], row['id'], submit_unheld)
                no_hold = True
            except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError, ValueError, TimeoutError) as exc:
                if attempted[0]:
                    self.last_reconcile_ok = False
                    raise ApiError(503, 'submission_unconfirmed', '任务提交结果未确认，等待核对') from exc
                try:
                    strict.restore_unsubmitted_claim(submission_snapshot[0])
                except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError) as restore_exc:
                    self.last_reconcile_ok = False
                    raise ApiError(503, 'submission_unconfirmed', '任务提交状态待核对') from restore_exc
                raise ApiError(409, 'priority_wait', '最高优先级任务等待控制器就绪') from exc
        else:
            code, out, err = run(argv, timeout=20)
        sid = out.split(";", 1)[0].strip()
        if strict is not None:
            # The scheduler call ran outside the broker lock. Every following
            # write rechecks the exact row captured by the locked claim.
            submitted = submission_snapshot[0]
            confirmed = False
            valid_sid = bool(re.fullmatch(r'[1-9][0-9]{0,9}', sid) and int(sid) <= 2**32-1)
            if code == 0 and valid_sid:
                try:
                    if not self._reconcile_set_scheduler_id(submitted, sid, top_held, strict):
                        raise ControllerUnavailable('task changed after scheduler submission')
                    self._reconcile_monthly_reservation(submitted, sid, False, top_held, strict)
                    if top_held:
                        receipt = strict.bind_held(row['owner_uid'], row['owner_name'], row['id'], int(sid), readonly=no_hold)
                        confirmed = self._reconcile_set_top_held(submitted, sid, receipt, strict)
                    else:
                        confirmed = self._reconcile_set_ordinary_state(submitted, sid, 'PENDING',
                            'waiting_for_scheduler', False, strict, reason_text='已提交 Slurm，等待资源')
                except (ReservationUncertain, ControllerUnavailable, OSError, sqlite3.Error, RuntimeError):
                    confirmed = False
                    if top_held and no_hold:
                        # The creation-bound job may have reached RUNNING before
                        # the pending receipt could be persisted. Only the exact
                        # controller RUNNING proof can complete this recovery.
                        try:
                            strict.reconcile_running_generation(row['owner_uid'], row['id'], int(sid))
                            confirmed = True
                        except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError, ValueError):
                            confirmed = False
            if not confirmed:
                # The request may already exist in Slurm. Retain its original
                # identity and stop this dispatch pass until reconciliation.
                self.last_reconcile_ok = False
                self.scheduler_error = '任务提交或绑定结果未确认，等待核对'
            self.store.audit(user.pw_uid, user.pw_name, 'dispatch', 'job', row['id'],
                             {'scheduler_id': sid if valid_sid else None, 'returncode': code},
                             'accepted' if confirmed else 'uncertain')
            current = self.store.db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
            if current is None:
                raise ApiError(409, 'submission_uncertain', '提交结果尚未确认，请等待核对')
            return self.job_json(current)
        if code == 0 and sid.isdigit():
            if selected is not None or top_held:
                # Persist the scheduler ID before binding. A lost reply or
                # process crash is reconciled against this same identity.
                changed = self.store.db.execute("UPDATE jobs SET scheduler_id=?,reason_code='binding_reservation',reason_text='正在确认调度绑定',updated_at=? WHERE id=? AND state='SUBMITTING' AND (scheduler_id IS NULL OR scheduler_id=?)", (sid, utc_now(), row["id"], sid)).rowcount
                if changed != 1:
                    return self.job_json(self.store.db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
            if selected is not None:
                try:
                    coordinator.bind(row["id"], row["owner_uid"], int(sid))
                except ReservationUncertain:
                    self.store.db.execute("UPDATE jobs SET reason_code='binding_uncertain',reason_text='月度预留绑定结果未确认，等待核对',updated_at=? WHERE id=?", (utc_now(), row["id"]))
                    self.store.audit(user.pw_uid, user.pw_name, "dispatch", "job", row["id"], {"scheduler_id":sid}, "uncertain")
                    return self.job_json(self.store.db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
            if top_held:
                try:
                    held_row = self.store.db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
                    if held_row is None:
                        raise ControllerUnavailable('submitted task disappeared')
                    receipt = strict.bind_held(row['owner_uid'], row['owner_name'], row['id'], int(sid))
                    if not self._reconcile_set_top_held(held_row, sid, receipt, strict):
                        raise ControllerUnavailable('submitted task changed during binding')
                except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError):
                    self.last_reconcile_ok = False
                    self.scheduler_error = '优先任务绑定待核对，保持挂起'
                    return self.job_json(self.store.db.execute("SELECT * FROM jobs WHERE id=?", (row['id'],)).fetchone())
            if not top_held:
                self.store.db.execute("UPDATE jobs SET state='PENDING',scheduler_id=?,reason_code='waiting_for_scheduler',reason_text='已提交 Slurm，等待资源',updated_at=? WHERE id=? AND state='SUBMITTING' AND (scheduler_id IS NULL OR scheduler_id=?)", (sid, utc_now(), row["id"], sid))
        elif selected is not None or code == 124 or code == 0:
            self.store.db.execute("UPDATE jobs SET reason_code='submission_uncertain',reason_text='提交结果未确认；保留请求和额度，后台核对，不重复提交',updated_at=? WHERE id=?", (utc_now(), row["id"]))
        else:
            self.store.db.execute("UPDATE jobs SET state='REJECTED',reason_code='scheduler_rejected',reason_text=?,updated_at=?,finished_at=? WHERE id=?", (err[:2000] or "调度器拒绝", utc_now(), utc_now(), row["id"]))
        self.store.audit(user.pw_uid, user.pw_name, "dispatch", "job", row["id"], {"scheduler_id": sid if sid.isdigit() else None, "returncode": code}, "accepted" if code == 0 and sid.isdigit() else "uncertain" if selected is not None or code in (0, 124) else "rejected")
        return self.job_json(self.store.db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def dispatch_waiting(self) -> None:
        if not self.last_reconcile_ok or not self.admission_available():
            return
        # Each bounded epoch scans a finite snapshot once. A separate limited
        # lane checks newly arrived higher-priority jobs before lower tiers.
        # Persisted sequence numbers survive VACUUM and cannot be reused.
        tier_rank = "CASE j.selected_tier WHEN 'debug' THEN 0 WHEN 'important' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END"
        priority_enabled = getattr(self, 'strict_admission', None) is not None
        revision = None
        if priority_enabled:
            try:
                revision_row = self.store.db.execute('SELECT revision FROM priority_lane_revision WHERE singleton=1').fetchone()
                if revision_row is None or type(revision_row[0]) is not int or revision_row[0] < 0:
                    raise RuntimeError('priority queue revision unavailable')
                revision = revision_row[0]
            except (sqlite3.Error, RuntimeError):
                self.last_reconcile_ok = False
                self.scheduler_error = '优先级队列状态未确认，等待核对'
                return
        rank = ("(CASE WHEN j.priority_marker_required=1 THEN 0 ELSE 4 END + (" + tier_rank + "))"
                if priority_enabled else tier_rank)
        lane_count = 8 if priority_enabled else 4
        epoch = getattr(self, '_dispatch_epoch', None)
        if epoch is not None and epoch.get('priority_enabled', False) != priority_enabled:
            epoch = None
        if epoch is not None and priority_enabled and epoch.get('priority_revision') != revision:
            # Revisit moved tasks once per finite epoch. Retaining all other
            # cursors prevents repeated promotions from starving later jobs.
            if not epoch.get('promotion_revisited', False):
                epoch['base'][:4] = [0]*4
                epoch['promotion_revisited'] = True
            epoch['priority_revision'] = revision
        if epoch is None:
            last = self.store.db.execute("SELECT max(d.seq) FROM jobs j JOIN job_dispatch_order d ON d.job_id=j.id WHERE j.origin='platform' AND j.state='WAITING_QUOTA'").fetchone()[0]
            if last is None:
                return
            epoch = {'end': last, 'base': [0]*lane_count, 'new': [last]*lane_count,
                     'new_left': [64]*lane_count, 'priority_enabled': priority_enabled,
                     'priority_revision': revision, 'promotion_revisited': False}
            self._dispatch_epoch = epoch
        rows = []
        for priority in range(lane_count):
            common = "SELECT j.*,d.seq AS dispatch_seq," + rank + " AS dispatch_rank FROM jobs j JOIN job_dispatch_order d ON d.job_id=j.id WHERE j.origin='platform' AND j.state='WAITING_QUOTA' AND " + rank + "=? AND d.seq>? "
            rows.extend(self.store.db.execute(common + "AND d.seq<=? ORDER BY d.seq LIMIT ?", (priority, epoch['base'][priority], epoch['end'], 64-len(rows))).fetchall())
            if len(rows) == 64:
                break
            if epoch['new_left'][priority]:
                new_rows = self.store.db.execute(common + "ORDER BY d.seq LIMIT ?", (priority, epoch['new'][priority], min(epoch['new_left'][priority],64-len(rows)))).fetchall()
                rows.extend(new_rows)
            if len(rows) == 64:
                break
        if not rows:
            self._dispatch_epoch = None
            return
        deadline = time.monotonic() + 1.0
        processed = 0
        for row in rows:
            if processed and time.monotonic() >= deadline:
                break
            priority, seq = row['dispatch_rank'], row['dispatch_seq']
            if seq <= epoch['end']:
                epoch['base'][priority] = seq
            else:
                epoch['new'][priority] = seq
                epoch['new_left'][priority] -= 1
            processed += 1
            try:
                ident = self.identity(row["owner_uid"])
                controller_cfg = self.config.get("monthly_controller", {})
                controller_active = isinstance(controller_cfg, dict) and controller_cfg.get("activation_enabled") is True
                # The legacy staging ledger intentionally blocks dispatch and
                # is not authoritative after the controller cutover. The live
                # controller readiness is checked again by validate/dispatch.
                monthly_block = None if controller_active else self.monthly.block_reason(row["owner_uid"])
                if monthly_block:
                    if not self._dispatch_note_waiting(row, 'monthly_quota_wait', monthly_block):
                        self.last_reconcile_ok = False
                        break
                    continue
                quota = self.quota(row["owner_uid"])
                usage = self.active_usage(row["owner_uid"])
                job = self.row_job(row)
                app = self.store.db.execute("SELECT * FROM applications WHERE id=?", (row["temporary_application_id"],)).fetchone() if row["temporary_application_id"] else None
                temporary = bool(app and app["status"] == "APPROVED" and app["bound_job_id"] == row["id"])
                expiry = approval_expired(app["expires_at"], dt.datetime.now(dt.timezone.utc)) if app else None
                if row["temporary_application_id"] and (not app or expiry is None):
                    if not self._dispatch_note_waiting(row, 'approval_unverified', '批准有效期不可核验，等待管理员处理'):
                        self.last_reconcile_ok = False
                        break
                    continue
                if app and (not temporary or expiry):
                    if row["selected_tier"] is not None or getattr(self, 'strict_admission', None) is not None:
                        self._fence_reservation_cancellation(row["id"], "EXPIRED")
                    else:
                        self.store.db.execute("UPDATE jobs SET state='EXPIRED',reason_code='approval_expired',reason_text='批准在运行前过期，需重新申请',finished_at=?,updated_at=? WHERE id=? AND state='WAITING_QUOTA'", (utc_now(), utc_now(), row["id"]))
                    continue
                cap = max(quota["gpu_limit"], job["gpu"]) if temporary else quota["gpu_limit"]
                if not temporary and (job["gpu"] > quota["gpu_limit"] or (row['policy_version'] == 1 and job["time_minutes"] > int(self.qos[job["qos"]]["max_minutes"]))):
                    if not self._dispatch_note_waiting(row, 'policy_changed', '规则已变更，任务超过当前额度或档位时限；请取消后重新申请'):
                        self.last_reconcile_ok = False
                        break
                    continue
                if usage["gpu"] + job["gpu"] > cap or usage["concurrent_jobs"] >= quota["concurrent_job_limit"]:
                    continue
                self.validate_job(ident, job, temporary=temporary, policy_version=row['policy_version'])
                self.dispatch_job(ident, row, job, temporary)
                if not self.last_reconcile_ok:
                    break
            except (ControllerUnavailable, sqlite3.Error, RuntimeError):
                self.last_reconcile_ok = False
                self.scheduler_error = '任务派发状态未确认，等待核对'
                break
            except (ApiError, KeyError, OSError) as exc:
                if not self.last_reconcile_ok:
                    break
                try:
                    if not self._dispatch_note_waiting(row, 'dispatch_blocked', str(exc)[:1000]):
                        self.last_reconcile_ok = False
                        break
                except Exception as note_exc:
                    if (getattr(self, 'strict_admission', None) is None and not
                            isinstance(note_exc, (ControllerUnavailable, sqlite3.Error, RuntimeError, OSError))):
                        raise
                    self.last_reconcile_ok = False
                    self.scheduler_error = '任务派发状态未确认，等待核对'
                    break
            except Exception:
                if getattr(self, 'strict_admission', None) is None:
                    raise
                self.last_reconcile_ok = False
                self.scheduler_error = '任务派发状态未确认，等待核对'
                break

    def submit(self, ident: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        uid = ident["uid"]
        strict = getattr(self, 'strict_admission', None)
        if strict is not None and not strict.runtime.cutover_ready:
            raise ApiError(503, 'priority_cutover_pending', '优先级控制等待现有任务结束')
        pending_marker = None
        request_id = payload.get("client_request_id")
        request_hash = None
        if strict is not None and request_id is None:
            raise ApiError(400, 'request_id_required', '提交任务需要稳定的请求编号，以便断线后安全重试')
        if request_id is not None:
            if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,80}", request_id):
                raise ApiError(400, "invalid_request_id", "请求标识无效")
            try:
                canonical = json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
            except (TypeError, ValueError):
                raise ApiError(400, "invalid_job", "任务请求无法规范化")
            request_hash = hashlib.sha256(canonical).hexdigest()
            prior = self.store.db.execute("SELECT * FROM jobs WHERE owner_uid=? AND client_request_id=?", (uid, request_id)).fetchone()
            if prior:
                if prior['client_request_hash'] != request_hash:
                    raise ApiError(409, "idempotency_conflict", "请求编号已使用或历史请求无法核对，请查看原任务", {"job_id":prior['id']})
                if strict is not None:
                    try:
                        strict.recover_marker(prior['id'])
                    except ControllerUnavailable as exc:
                        raise ApiError(503, 'priority_admission_unknown', '任务已入队，优先级登记待核对，请勿重复提交',
                                       {'job_id': prior['id'], 'client_request_id': request_id}) from exc
                return self.job_json(prior)
            if strict is not None:
                try:
                    pending_marker = strict.pending_marker(uid, request_id, request_hash)
                except ControllerUnavailable as exc:
                    raise ApiError(503, 'priority_admission_unknown', '任务登记结果待核对，请勿重复提交',
                                   {'client_request_id': request_id}) from exc
        self.tick()
        if not self.scheduler_available() or not self.admission_available():
            raise ApiError(503, "scheduler_unavailable", "调度器或受控提交插件尚未就绪")
        app_id = payload.get("temporary_application_id")
        if app_id == "":
            app_id = None
        if app_id is not None and not isinstance(app_id, str):
            raise ApiError(400, "invalid_temporary_authorization", "批准标识无效")
        app = None
        policy_version = 2
        if app_id:
            app = self.store.db.execute("SELECT * FROM applications WHERE id=? AND owner_uid=?", (str(app_id), uid)).fetchone()
            if app:
                policy_version = app['policy_version']
                if policy_version not in (1, 2):
                    raise ApiError(409, 'invalid_policy', '申请计费版本无效')
            if app and app["bound_job_id"]:
                prior = self.store.db.execute("SELECT * FROM jobs WHERE id=?", (app["bound_job_id"],)).fetchone()
                if prior:
                    if request_id is not None and request_id != prior["client_request_id"]:
                        raise ApiError(409, "idempotency_conflict", "批准任务已绑定其他请求编号，请查看原任务", {"job_id":prior["id"]})
                    existing = self.row_job(prior)
                    existing["selected_tier"] = prior["selected_tier"]
                    for key, value in existing.items():
                        if key not in payload:
                            continue
                        supplied = payload[key]
                        try:
                            if key in ("gpu", "cpu", "memory_mb", "time_minutes"):
                                supplied = int(supplied)
                            elif key == "command" and isinstance(supplied, str):
                                supplied = supplied.strip()
                            elif key == "output_path" and not supplied:
                                supplied = os.path.join(existing["workdir"], "slurm-%j.out")
                            elif key in ("name", "environment"):
                                supplied = str(supplied)
                        except (TypeError, ValueError):
                            raise ApiError(409, "authorization_binding_mismatch", "批准绑定的任务已变更，必须重新申请")
                        if supplied != value:
                            raise ApiError(409, "authorization_binding_mismatch", "批准绑定的任务已变更，必须重新申请")
                    if strict is not None:
                        try:
                            strict.recover_marker(prior['id'])
                        except ControllerUnavailable as exc:
                            raise ApiError(503, 'priority_admission_unknown', '任务已入队，优先级登记待核对，请勿重复提交',
                                           {'job_id': prior['id'], 'client_request_id': prior['client_request_id']}) from exc
                    return self.job_json(prior)
            if not app or app["kind"] != "temporary_job" or app["status"] != "APPROVED" or app["authorization_used"]:
                raise ApiError(409, "invalid_temporary_authorization", "批准不存在、未批准或已使用")
            expiry = approval_expired(app["expires_at"], dt.datetime.now(dt.timezone.utc))
            if expiry is None:
                raise ApiError(409, "temporary_authorization_unverified", "批准有效期不可核验，请联系管理员")
            if expiry:
                raise ApiError(409, "temporary_authorization_expired", "批准已过期")
            approved = json_object(app["approved_json"], {})
            job = self.validate_job(ident, approved, temporary=True, policy_version=policy_version)
            if policy_version == 2 and (approved.get('time_minutes') != job['time_minutes'] or approved.get('qos') != job['qos']):
                raise ApiError(409, 'authorization_binding_mismatch', '批准绑定的计费参数不一致')
            supplied = {key: payload[key] for key in job if key in payload}
            if supplied and any(supplied[key] != job[key] for key in supplied):
                raise ApiError(409, "authorization_binding_mismatch", "批准绑定的任务已变更，必须重新申请")
        else:
            normalized, _ = self.normalize_legacy_defaults(payload)
            job = self.validate_job(ident, normalized, policy_version=policy_version)
            if job["gpu"] > self.quota(uid)["gpu_limit"]:
                raise ApiError(409, "permanent_quota_exceeded", "单任务 GPU 请求超过个人上限，请申请临时资源")
        if strict is not None and job.get('selected_tier') is None:
            raise ApiError(409, 'selected_tier_required', '优先级控制要求任务选择月度额度档位')
        usage = self.active_usage(uid)
        if usage["active_jobs"] >= self.quota(uid)["active_job_limit"]:
            raise ApiError(409, "active_job_limit", "活动任务数量已满，包括运行和排队任务；请先取消或等待任务结束")
        jid = pending_marker or uuid.uuid4().hex
        # This is a snapshot of validated job fields, not proof of the final
        # script, scheduler job, account generation, or dispatch authority.
        try:
            effective_digest = self.effective_job_digest(jid, uid, job, app_id)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "invalid_job", "任务请求无法规范化") from exc
        db = self.store.db
        def insert_job(db):
            if app:
                changed = db.execute("UPDATE applications SET authorization_used=1,bound_job_id=?,updated_at=? WHERE id=? AND status='APPROVED' AND authorization_used=0 AND bound_job_id IS NULL AND policy_version=?", (jid, utc_now(), app_id, policy_version)).rowcount
                if changed != 1:
                    raise ApiError(409, "authorization_consumed", "批准已被其他请求使用")
            db.execute("INSERT INTO jobs(id,owner_uid,owner_name,name,state,requested_gpu,requested_cpu,requested_mem_mb,requested_minutes,qos,workdir,env_name,command,output_path,reason_code,reason_text,temporary_application_id,created_at,updated_at,client_request_id,selected_tier,client_request_hash,effective_job_digest_version,effective_job_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (jid, uid, ident["username"], job["name"], "WAITING_QUOTA", job["gpu"], job["cpu"], job["memory_mb"], job["time_minutes"], job["qos"], job["workdir"], job["environment"], job["command"], job["output_path"], "waiting_user_quota", "已入队，等待个人额度与设备资源", app_id, utc_now(), utc_now(), request_id, job.get("selected_tier"), request_hash, 1, effective_digest))
            policy_digest = hashlib.sha256(('monthly-policy:2:' + effective_digest).encode('ascii')).hexdigest() if policy_version == 2 else None
            db.execute("UPDATE jobs SET policy_version=?,policy_digest=? WHERE id=?", (policy_version, policy_digest, jid))
            self.store.audit(uid, ident["username"], "queue", "job", jid, {"temporary_application_id": app_id}, "accepted")
        if strict is not None:
            try:
                strict.enqueue(uid, ident['username'], jid, request_id, request_hash, effective_digest, insert_job)
            except ControllerUnavailable as exc:
                raise ApiError(503, 'priority_admission_unknown', '任务登记结果待核对，请查看队列并勿重复提交',
                               {'job_id': jid, 'client_request_id': request_id}) from exc
        else:
            db.execute("BEGIN IMMEDIATE")
            try:
                insert_job(db)
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        self.dispatch_waiting()
        return self.job_json(db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())

    def render_script(self, jid: str, user: pwd.struct_passwd, job: Dict[str, Any]) -> str:
        lines = ["#!/bin/bash", "set -euo pipefail",
                 "export HOME=" + shell_quote(user.pw_dir),
                 "export USER=" + shell_quote(user.pw_name), "export LOGNAME=" + shell_quote(user.pw_name),
                 "export PATH=" + shell_quote(user.pw_dir + "/.local/bin:/usr/local/bin:/usr/bin:/bin"),
                 "export SLURM_EXPORT_ENV=ALL", "cd -- " + shell_quote(job["workdir"])]
        env = job["environment"]
        if env == "system":
            lines.append("exec /bin/bash -lc " + shell_quote(job["command"]))
        elif env.startswith("/"):
            lines.extend(["export PATH=" + shell_quote(env.rstrip("/") + "/bin") + ':"$PATH"', "exec /bin/bash -c " + shell_quote(job["command"])])
        else:
            lines.extend(['for c in "$HOME/miniconda3/bin/conda" "$HOME/anaconda3/bin/conda" /opt/conda/bin/conda; do',
                          '  if [ -x "$c" ]; then exec "$c" run --no-capture-output -n ' + shell_quote(env) + ' /bin/bash -c ' + shell_quote(job["command"]) + '; fi',
                          'done', 'echo "Conda executable not found for this user" >&2', 'exit 78'])
        return "\n".join(lines) + "\n"

    def job_json(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {**{k: row[k] for k in row.keys()}, "managed": row["origin"] == "platform", "usage_gpu_seconds": self.monthly.job_usage(row)}

    def jobs(self, ident: Dict[str, Any]) -> List[Dict[str, Any]]:
        self.tick()
        if ident["is_admin"]:
            rows = self.store.db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1000").fetchall()
        else:
            rows = self.store.db.execute("SELECT * FROM jobs WHERE owner_uid=? ORDER BY created_at DESC LIMIT 1000", (ident["uid"],)).fetchall()
        return [self.job_json(x) for x in rows]

    def _fence_reservation_cancellation(self, jid: str, target: str) -> bool:
        if target not in ("CANCELLED", "EXPIRED"):
            raise ValueError("invalid cancellation target")
        strict = getattr(self, 'strict_admission', None)
        db = self.store.db
        def update_locked():
            if strict is not None:
                result = strict.begin_cancel_in_transaction(jid, target, utc_now())
                if result is not None:
                    return result
                # Ordinary cancellation and priority adoption must make their
                # decision under the same lock and database transaction.
                current = db.execute("SELECT selected_tier FROM jobs WHERE id=? AND state='WAITING_QUOTA' AND scheduler_id IS NULL", (jid,)).fetchone()
                if current is None:
                    return False
                if current['selected_tier'] is None:
                    return db.execute("UPDATE jobs SET state=?,cancel_target=?,reason_code=?,reason_text=?,updated_at=?,finished_at=? "
                                      "WHERE id=? AND state='WAITING_QUOTA' AND scheduler_id IS NULL AND selected_tier IS NULL AND priority_marker_required=0",
                                      (target, target, 'approval_expired' if target == 'EXPIRED' else 'cancelled_by_user',
                                       '已取消排队' if target == 'CANCELLED' else '批准已过期', utc_now(), utc_now(), jid)).rowcount == 1
            return db.execute("UPDATE jobs SET state='CANCELLING_RESERVATION',cancel_target=?,reason_code='cancelling',reason_text='正在核对月度预留',updated_at=? WHERE id=? AND state='WAITING_QUOTA' AND scheduler_id IS NULL", (target, utc_now(), jid)).rowcount == 1
        if strict is None or db.in_transaction:
            return update_locked()
        with strict.runtime.coordinator.serialized():
            db.execute('BEGIN IMMEDIATE')
            try:
                changed = update_locked()
                db.execute('COMMIT')
                return changed
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def _strict_cancel_reservation(self, row, strict):
        """Write the cancellation tombstone against the verified controller."""
        db = self.store.db
        with strict.runtime.coordinator.serialized():
            if db.in_transaction or not strict.runtime.reconciled_for_current_boot():
                raise ReservationUncertain('priority prerequisites unavailable')
            current = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
            if (current is None or dict(current) != dict(row) or current['state'] != 'CANCELLING_RESERVATION'
                    or current['scheduler_id'] is not None or
                    not strict._verify_effective_job(row['owner_uid'], row['id'], current['effective_job_digest'])):
                raise ReservationUncertain('cancellation task changed')
            marker = strict._has_marker_locked(row['id'])
            strict._same_controller()
            transport = strict.runtime.coordinator.transport
            before = transport.status()
            source, peer = self.verified_monthly_bridge(), strict.bridge
            if (source.path, source.controller_uid, source.expected_pid, source.release_manifest) != (
                    peer.path, peer.controller_uid, peer.expected_pid, peer.release_manifest):
                raise ReservationUncertain('reservation controller differs from priority controller')
            pinned = PinnedControllerBridge(source.path, source.controller_uid, source.expected_pid,
                                            source.timeout, release_manifest=source.release_manifest)
            if not pinned.readiness():
                raise ReservationUncertain('reservation controller unavailable')
            ReservationCoordinator(db, pinned).cancel(row['id'], row['owner_uid'], ensure_tombstone=marker)
            after = transport.status()
            current = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
            if (any(after.get(k) != before.get(k) for k in ('epoch', 'boot', 'watermark')) or
                    not strict.runtime.reconciled_for_current_boot() or
                    (marker and 'fenced' in after and after['fenced'] is not True) or
                    current is None or dict(current) != dict(row) or
                    strict._has_marker_locked(row['id']) is not marker or
                    not strict._verify_effective_job(row['owner_uid'], row['id'], current['effective_job_digest'])):
                raise ReservationUncertain('controller or task changed during cancellation')
            return marker

    def reconcile_reservation_cancellations(self) -> None:
        try:
            self._reconcile_reservation_cancellations_once()
        except Exception:
            if getattr(self, 'strict_admission', None) is not None:
                self.last_reconcile_ok = False
                self.scheduler_error = '预留取消未确认，等待核对'
            raise

    def _reconcile_reservation_cancellations_once(self) -> None:
        rows = self.store.db.execute("SELECT * FROM jobs WHERE state='CANCELLING_RESERVATION' AND scheduler_id IS NULL").fetchall()
        for row in rows:
            strict = getattr(self, 'strict_admission', None)
            try:
                if strict is not None:
                    strict_marker = self._strict_cancel_reservation(row, strict)
                else:
                    strict_marker = False
                    self.monthly_reservation_coordinator().cancel(row["id"], row["owner_uid"])
            except Exception as exc:
                if strict is not None:
                    self.last_reconcile_ok = False
                    self.scheduler_error = self.scheduler_error or '预留取消未确认，等待核对'
                else:
                    if not isinstance(exc, (ReservationUncertain, ControllerUnavailable, OSError)):
                        raise
                    self.store.db.execute("UPDATE jobs SET reason_code='cancellation_unconfirmed',reason_text='预留释放未确认，等待核对',updated_at=? WHERE id=? AND state='CANCELLING_RESERVATION'", (utc_now(), row["id"]))
                continue
            if strict_marker:
                try:
                    if strict.finish_cancel_waiting(row['id'], utc_now()) is not True:
                        self.last_reconcile_ok = False
                        self.scheduler_error = self.scheduler_error or '优先任务取消未确认，等待核对'
                    continue
                except Exception:
                    self.last_reconcile_ok = False
                    self.scheduler_error = self.scheduler_error or '优先任务取消未确认，等待核对'
                    continue
            target = row["cancel_target"]
            if target not in ("CANCELLED", "EXPIRED"):
                if strict is not None:
                    self.last_reconcile_ok = False
                else:
                    self.store.db.execute("UPDATE jobs SET reason_code='cancellation_unconfirmed',reason_text='取消目标不明，等待管理员核对',updated_at=? WHERE id=?", (utc_now(), row["id"]))
                continue
            reason = "批准已过期并确认释放预留" if target == "EXPIRED" else "已取消排队并确认释放预留"
            sql = "UPDATE jobs SET state=?,reason_code=?,reason_text=?,updated_at=?,finished_at=? WHERE id=? AND state='CANCELLING_RESERVATION'"
            params = (target, "approval_expired" if target == "EXPIRED" else "cancelled_by_user", reason, utc_now(), utc_now(), row["id"])
            if strict is None:
                self.store.db.execute(sql, params)
                continue
            db = self.store.db
            try:
                with strict.runtime.coordinator.serialized():
                    db.execute('BEGIN IMMEDIATE')
                    try:
                        current = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
                        if (current is None or dict(current) != dict(row) or
                                strict._has_marker_locked(row['id']) is not False or
                                not strict._verify_effective_job(row['owner_uid'], row['id'], current['effective_job_digest'])):
                            db.execute('ROLLBACK')
                            self.last_reconcile_ok = False
                            continue
                        if db.execute(sql, params).rowcount != 1:
                            raise ReservationUncertain('cancellation state changed')
                        db.execute('COMMIT')
                    except BaseException:
                        if db.in_transaction:
                            db.execute('ROLLBACK')
                        raise
            except Exception:
                self.last_reconcile_ok = False

    def cancel(self, ident: Dict[str, Any], jid: str) -> Dict[str, Any]:
        try:
            return self._cancel_once(ident, jid)
        except ApiError as exc:
            if (getattr(self, 'strict_admission', None) is not None and exc.code in
                    ('submission_uncertain', 'priority_cancel_unconfirmed', 'scheduler_identity_unconfirmed')):
                self.last_reconcile_ok = False
                self.scheduler_error = '任务取消状态未确认，等待核对'
            raise
        except Exception:
            if getattr(self, 'strict_admission', None) is not None:
                self.last_reconcile_ok = False
                self.scheduler_error = '任务取消状态未确认，等待核对'
            raise

    def _cancel_once(self, ident: Dict[str, Any], jid: str) -> Dict[str, Any]:
        self.tick()
        row = self.store.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            raise ApiError(404, "job_not_found", "任务不存在")
        if row["owner_uid"] != ident["uid"] and not ident["is_admin"]:
            raise ApiError(403, "job_forbidden", "只能操作自己的任务")
        if row["origin"] != "platform":
            raise ApiError(409, "external_job_read_only", "外部 Slurm 任务仅查看；请使用原有 Slurm 管理工具操作")
        if row["state"] not in ACTIVE_STATES:
            return self.job_json(row)
        if row["state"] == "SUBMITTING" and not row["scheduler_id"]:
            raise ApiError(409, "submission_uncertain", "提交结果尚未确认，不能假报取消成功；请稍后核对")
        strict = getattr(self, 'strict_admission', None)
        # Unsubmitted strict jobs must be classified inside the same lock and
        # write transaction as cancellation. A prepared promotion fence may
        # legitimately exist before its marker and still needs a durable request.
        if (row["selected_tier"] is not None or strict is not None) and not row["scheduler_id"]:
            if row["state"] == "WAITING_QUOTA":
                if not self._fence_reservation_cancellation(jid, "CANCELLED"):
                    raise ApiError(409, "submission_uncertain", "任务状态已变化，请稍后核对")
            elif row["state"] != "CANCELLING_RESERVATION":
                raise ApiError(409, "submission_uncertain", "任务状态未确认，无法取消预留")
            self.reconcile_reservation_cancellations()
            self.store.audit(ident["uid"], ident["username"], "cancel", "job", jid, {}, "requested")
            return self.job_json(self.store.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
        strict_marker = strict is not None and strict.has_marker(jid)
        if strict_marker and row['scheduler_id']:
            try:
                strict.cancel_submitted_pending(row['owner_uid'], jid, int(row['scheduler_id']))
            except (ControllerUnavailable, OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
                raise ApiError(409, 'priority_cancel_unconfirmed', '优先任务取消待核对') from exc
            self.last_reconcile_ok = False
            self.scheduler_error = '优先任务取消待最终核对'
            return self.job_json(self.store.db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())
        if strict is not None and row['scheduler_id']:
            # A recycled Slurm job ID could address another user's running job.
            # Wait for an exact controller instance proof before scancel.
            raise ApiError(409, 'scheduler_identity_unconfirmed', '任务调度实例尚需核对，暂不能安全取消')
        if row["scheduler_id"]:
            code, _, err = run(["scancel", row["scheduler_id"]], timeout=8)
            if code:
                raise ApiError(502, "cancel_failed", err or "取消失败")
            self.store.db.execute("UPDATE jobs SET reason_code='cancellation_requested',reason_text='已请求 Slurm 取消，等待确认',updated_at=? WHERE id=?", (utc_now(), jid))
            self.reconcile()
        else:
            self.store.db.execute("UPDATE jobs SET state='CANCELLED',reason_code='cancelled_by_user',reason_text='已取消排队',updated_at=?,finished_at=? WHERE id=?", (utc_now(), utc_now(), jid))
        self.store.audit(ident["uid"], ident["username"], "cancel", "job", jid, {}, "requested")
        return self.job_json(self.store.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())

    def log(self, ident: Dict[str, Any], jid: str) -> Dict[str, Any]:
        row = self.store.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            raise ApiError(404, "job_not_found", "任务不存在")
        if row["owner_uid"] != ident["uid"] and not ident["is_admin"]:
            raise ApiError(403, "job_forbidden", "只能查看自己的任务日志")
        if row["origin"] != "platform":
            raise ApiError(409, "external_job_read_only", "外部 Slurm 任务的日志路径尚未验证，请使用原有 Slurm 工具查看")
        if not row["scheduler_id"]:
            return {"job_id": jid, "available": False, "content": "", "reason": "任务尚未提交给调度器"}
        path = row["output_path"].replace("%j", row["scheduler_id"])
        owner = self.user(row["owner_uid"])
        # Open every component with openat/O_NOFOLLOW, under the job owner's UID.
        # No privileged read, no symlink race, no FIFO/device reads, bounded tail.
        reader = """import os,sys,stat,json
p=sys.argv[1]; roots=json.loads(sys.argv[2]); root=next((r for r in roots if p.startswith(r.rstrip('/')+'/')),None)
if root is None: raise SystemExit(3)
fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
parts=p.split('/')[1:]
try:
 for part in parts[:-1]:
  nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd); os.close(fd); fd=nxt
 f=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
 st=os.fstat(f)
 if not stat.S_ISREG(st.st_mode): raise SystemExit(3)
 os.lseek(f,max(0,st.st_size-1048576),os.SEEK_SET)
 sys.stdout.buffer.write(os.read(f,1048576)); os.close(f)
finally: os.close(fd)
"""
        code, out, err = run(["/usr/bin/python3", "-c", reader, path, json.dumps([str(x) for x in self.user_roots(owner)])], timeout=5, uid=owner.pw_uid, strip=False)
        if code and "No such file" in err:
            return {"job_id": jid, "path": path, "available": False, "content": "", "reason": "日志尚未生成"}
        if code:
            raise ApiError(403, "log_read_failed", "日志不可读或路径含符号链接")
        return {"job_id": jid, "path": path, "available": True, "content": out}

    def add_notification(self, uid: int, kind: str, title: str, body: str) -> None:
        self.store.db.execute("INSERT INTO notifications(id,recipient_uid,kind,title,body,created_at) VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, uid, kind, title, body, utc_now()))

    def applications(self, ident: Dict[str, Any]) -> List[Dict[str, Any]]:
        self.expire_applications()
        if ident["is_admin"]:
            rows = self.store.db.execute("SELECT * FROM applications ORDER BY created_at DESC").fetchall()
        else:
            rows = self.store.db.execute("SELECT * FROM applications WHERE owner_uid=? ORDER BY created_at DESC", (ident["uid"],)).fetchall()
        return [{**dict(r), "requested": json_object(r["requested_json"], {}), "approved": json_object(r["approved_json"], None)} for r in rows]

    def expire_applications(self) -> None:
        try:
            self._expire_applications_once()
        except Exception:
            if getattr(self, 'strict_admission', None) is not None:
                self.last_reconcile_ok = False
                self.scheduler_error = '批准到期状态未确认，等待核对'
            raise

    def _expire_applications_once(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        for row in self.store.db.execute("SELECT * FROM applications WHERE kind='temporary_job' AND status='APPROVED' AND expires_at IS NOT NULL").fetchall():
            expired = approval_expired(row["expires_at"], now)
            if expired is None:
                print("Invalid approval expiry for %r; keeping it closed" % row["id"], file=sys.stderr)
                continue
            if not expired:
                continue
            db = self.store.db
            strict = getattr(self, 'strict_admission', None)
            lock = strict.runtime.coordinator.serialized() if strict is not None else nullcontext()
            with lock:
                db.execute("BEGIN IMMEDIATE")
                try:
                    # The initial scan can become stale before the write lock.
                    # Re-read under that lock before fencing a job or expiring an
                    # approval that may have been renewed by another connection.
                    current = db.execute("SELECT * FROM applications WHERE id=? AND kind='temporary_job' AND status='APPROVED'", (row["id"],)).fetchone()
                    if not current or approval_expired(current["expires_at"], dt.datetime.now(dt.timezone.utc)) is not True:
                        db.execute("COMMIT")
                        continue
                    row = current
                    job = db.execute("SELECT * FROM jobs WHERE id=?", (row["bound_job_id"],)).fetchone() if row["bound_job_id"] else None
                    # Submission's WAITING -> SUBMITTING claim uses the same DB;
                    # never expire or cancel an already claimed scheduler job.
                    if job and job["state"] != "WAITING_QUOTA":
                        db.execute("COMMIT")
                        continue
                    priority_fenced = False
                    if job and strict is not None:
                        priority_fenced = strict.begin_cancel_in_transaction(job['id'], 'EXPIRED', utc_now()) is True
                    if priority_fenced:
                        pass
                    elif job and job["selected_tier"] is not None:
                        if not self._fence_reservation_cancellation(job["id"], "EXPIRED"):
                            raise RuntimeError("unable to fence expired reservation")
                    elif job:
                        db.execute("UPDATE jobs SET state='EXPIRED',reason_code='approval_expired',reason_text='批准在提交调度器前已过期',finished_at=?,updated_at=? WHERE id=? AND state='WAITING_QUOTA'", (utc_now(), utc_now(), job["id"]))
                    db.execute("UPDATE applications SET status='EXPIRED',updated_at=? WHERE id=? AND status='APPROVED'", (utc_now(), row["id"]))
                    db.execute("COMMIT")
                except Exception:
                    db.execute("ROLLBACK")
                    raise
            self.add_notification(row["owner_uid"], "application_expired", "批准已过期", "未提交给调度器的授权已失效；已提交的任务不受影响。")

    def monthly_controller_bridge(self) -> ControllerBridge:
        cfg = self.config.get("monthly_controller", {})
        if not isinstance(cfg, dict) or cfg.get("grant_enabled") is not True:
            raise ApiError(409, "monthly_not_enabled", "月度额度追加尚未启用")
        try:
            return self.verified_monthly_bridge()
        except ControllerUnavailable as exc:
            raise ApiError(503, "controller_unavailable", "控制器配置不可用") from exc

    def finish_monthly_grant(self, ident: Dict[str, Any], aid: str) -> Dict[str, Any]:
        self.require_admin(ident)
        row = self.store.db.execute("SELECT * FROM monthly_grant_outbox WHERE application_id=?", (aid,)).fetchone()
        if not row:
            raise ApiError(404, "grant_not_found", "追加额度记录不存在")
        if row["state"] == "APPLIED":
            return dict(self.store.db.execute("SELECT * FROM applications WHERE id=?", (aid,)).fetchone())
        if row["state"] != "PENDING":
            raise ApiError(409, "grant_state_inconsistent", "追加额度状态不明，需管理员核对")
        bridge = self.monthly_controller_bridge()
        try:
            bridge.grant(row["target_uid"], row["month"], row["gpu_seconds"], aid, "uid-%s" % row["actor_uid"])
        except (ControllerUnavailable, ValueError) as exc:
            raise ApiError(503, "grant_unconfirmed", "控制器结果未确认，保留原申请等待核对", {"application_id":aid}) from exc
        db = self.store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            changed = db.execute("UPDATE monthly_grant_outbox SET state='APPLIED',updated_at=? WHERE application_id=? AND state='PENDING'", (utc_now(),aid)).rowcount
            if changed:
                applied = db.execute("UPDATE applications SET status='APPROVED',updated_at=? WHERE id=? AND status='GRANT_PENDING'", (utc_now(),aid)).rowcount
                if applied != 1:
                    raise ApiError(409, "grant_state_inconsistent", "控制器可能已生效，但申请状态不一致，需管理员核对")
                self.store.audit(ident["uid"],ident["username"],"grant_applied","application",aid,{"approved_by_uid":row["actor_uid"]},"confirmed")
                self.add_notification(row["target_uid"],"application_decision","额度追加已生效","控制器已确认本月额度追加")
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        return dict(db.execute("SELECT * FROM applications WHERE id=?", (aid,)).fetchone())

    def monthly_request(self, requested: Any) -> Dict[str, Any]:
        if not isinstance(requested,dict):
            raise ApiError(400,"invalid_monthly_request","月度追加申请必须是对象")
        hours = requested.get("gpu_hours")
        if isinstance(hours,bool) or not isinstance(hours,int) or not 1 <= hours <= 100000:
            raise ApiError(400,"invalid_gpu_hours","追加 GPU 小时必须为 1 至 100000 的整数")
        month = requested.get("month")
        if month != month_now():
            raise ApiError(409,"month_changed","月份已切换，请刷新后重新申请本月额度")
        key = requested.get("client_request_id")
        if not isinstance(key,str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}",key):
            raise ApiError(400,"request_id_required","月度追加申请需要稳定请求编号")
        return {"gpu_hours":hours,"month":month,"client_request_id":key}

    def create_application(self, ident: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        kind = payload.get("kind")
        if kind not in ("temporary_job", "quota_change", "monthly_gpu_hours"):
            raise ApiError(400, "invalid_application", "未知申请类型")
        reason = str(payload.get("reason", "")).strip()
        if len(reason) < 5 or len(reason) > 2000:
            raise ApiError(400, "reason_required", "理由长度需为 5 到 2000 字符")
        requested = payload.get("requested")
        application_policy = 1
        if kind == "monthly_gpu_hours":
            if not self.monthly_controller_snapshot(ident["uid"]).get("request_available"):
                raise ApiError(409,"monthly_not_enabled","月度额度追加暂不可用")
            requested = self.monthly_request(requested)
            prior = self.store.db.execute("SELECT a.* FROM monthly_requests r JOIN applications a ON a.id=r.application_id WHERE r.uid=? AND r.request_id=?", (ident["uid"],requested["client_request_id"])).fetchone()
            if prior:
                old = json_object(prior["requested_json"], {})
                if old != requested or prior["reason"] != reason:
                    raise ApiError(409,"idempotency_conflict","同一请求编号不能对应不同申请")
                return dict(prior)
        elif kind == "temporary_job":
            if not self.config.get("temporary_authorizations", {}).get("enabled", False):
                raise ApiError(409, "temporary_authorization_disabled", "临时资源授权尚未开启")
            if not isinstance(requested, dict):
                raise ApiError(400, "invalid_job", "任务必须是对象")
            requested, _ = self.normalize_legacy_defaults(requested)
            application_policy = 2
            requested = self.validate_job(ident, requested, temporary=True, policy_version=application_policy)
        else:
            requested = self.quota_value(requested)
        if self.store.db.execute("SELECT COUNT(*) FROM applications WHERE owner_uid=? AND status='PENDING'", (ident["uid"],)).fetchone()[0] >= 32:
            raise ApiError(409, "pending_application_limit", "待审批申请已达 32 项")
        aid = uuid.uuid4().hex
        db=self.store.db; db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("INSERT INTO applications(id,owner_uid,owner_name,kind,status,requested_json,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (aid, ident["uid"], ident["username"], kind, "PENDING", json.dumps(requested, sort_keys=True), reason, utc_now(), utc_now()))
            if kind == 'temporary_job':
                db.execute("UPDATE applications SET policy_version=? WHERE id=?", (application_policy, aid))
            if kind == "monthly_gpu_hours":
                db.execute("INSERT INTO monthly_requests VALUES(?,?,?)", (ident["uid"],requested["client_request_id"],aid))
            self.store.audit(ident["uid"], ident["username"], "create", "application", aid, {"kind":kind}, "pending")
            for uid in self.admin_uids():
                self.add_notification(uid, "application_pending", "有新的资源申请", ident["username"] + "：" + reason)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK"); raise
        return dict(db.execute("SELECT * FROM applications WHERE id=?", (aid,)).fetchone())

    def admin_uids(self) -> List[int]:
        try:
            g = grp.getgrnam(self.admin_group)
        except KeyError:
            return []
        result = set()
        for name in g.gr_mem:
            try: result.add(pwd.getpwnam(name).pw_uid)
            except KeyError: pass
        # Primary group members are not listed in gr_mem.
        for p in pwd.getpwall():
            if p.pw_gid == g.gr_gid: result.add(p.pw_uid)
        return sorted(result)

    def decide_application(self, ident: Dict[str, Any], aid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.require_admin(ident)
        row = self.store.db.execute("SELECT * FROM applications WHERE id=?", (aid,)).fetchone()
        if not row:
            raise ApiError(404, "application_not_found", "申请不存在")
        if row["status"] != "PENDING":
            raise ApiError(409, "application_already_decided", "申请已处理，不能重复批准或改变结论")
        action = payload.get("action")
        if action not in ("approve", "approve_adjusted", "reject"):
            raise ApiError(400, "invalid_decision", "未知审批动作")
        reason = str(payload.get("decision_reason", "")).strip()
        if (action in ("reject", "approve_adjusted") and not reason) or len(reason)>2000:
            raise ApiError(400, "decision_reason_required", "拒绝或调整批准需要填写理由")
        approved = json_object(row["requested_json"], {})
        expiry = None
        if row["kind"] == "monthly_gpu_hours" and action != "reject":
            if not self.monthly_controller_snapshot(row["owner_uid"]).get("enforcement_ready"):
                raise ApiError(409, "monthly_not_enabled", "月度启动限制尚未启用，暂不能批准追加额度")
            self.monthly_controller_bridge()
            adjusted = payload.get("approved", {}) if action == "approve_adjusted" else {}
            if not isinstance(adjusted, dict) or set(adjusted) - {"gpu_hours"}:
                raise ApiError(400, "invalid_approved", "仅允许调整追加 GPU 小时")
            approved = self.monthly_request({**approved, **adjusted})
            db = self.store.db
            db.execute("BEGIN IMMEDIATE")
            try:
                changed = db.execute("UPDATE applications SET status='GRANT_PENDING',approved_json=?,decision_reason=?,updated_at=? WHERE id=? AND status='PENDING'", (json.dumps(approved,sort_keys=True),reason or "管理员批准",utc_now(),aid)).rowcount
                if changed != 1: raise ApiError(409,"application_already_decided","申请已处理")
                db.execute("INSERT INTO monthly_grant_outbox VALUES(?,?,?,?,?,'PENDING',?,?)", (aid,row["owner_uid"],int(approved["month"].replace('-','')),approved["gpu_hours"]*3600,ident["uid"],utc_now(),utc_now()))
                self.store.audit(ident["uid"],ident["username"],action,"application",aid,{"approved":approved,"reason":reason},"grant_pending")
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
            return self.finish_monthly_grant(ident, aid)
        if action != "reject":
            adjusted = payload.get("approved", {}) if action == "approve_adjusted" else {}
            if not isinstance(adjusted, dict):
                raise ApiError(400, "invalid_approved", "批准内容必须是对象")
            if row["kind"] == "temporary_job":
                application_policy = row['policy_version']
                if application_policy not in (1, 2):
                    raise ApiError(409, 'invalid_policy', '申请计费版本无效')
                if application_policy == 2:
                    if (type(approved.get('time_minutes')) is not int or approved['time_minutes'] != 4294967295 or
                            not isinstance(approved.get('qos'), str) or not SAFE_NAME.fullmatch(approved['qos'])):
                        raise ApiError(409, 'invalid_policy', '申请的无限时长或 QoS 快照无效')
                    if any(key in adjusted and (type(adjusted[key]) is not type(approved[key]) or adjusted[key] != approved[key])
                           for key in ('time_minutes', 'qos')):
                        raise ApiError(400, 'immutable_policy_fields', '不能调整无限时长任务的时限或 QoS')
                for key in (("gpu", "cpu", "memory_mb") if application_policy == 2 else ("gpu", "cpu", "memory_mb", "time_minutes", "qos")):
                    if key in adjusted: approved[key] = adjusted[key]
                approved = self.validate_job(self.identity(row["owner_uid"]), approved, temporary=True, policy_version=application_policy)
                valid = int(payload.get("valid_minutes", 1440))
                if not 1 <= valid <= 10080:
                    raise ApiError(400, "invalid_expiry", "有效期须为 1 到 10080 分钟")
                expiry=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(minutes=valid)).isoformat()
            else:
                approved=self.quota_value({**approved, **adjusted})
        status="REJECTED" if action == "reject" else "APPROVED"
        db=self.store.db; db.execute("BEGIN IMMEDIATE")
        try:
            changed=db.execute("UPDATE applications SET status=?,approved_json=?,decision_reason=?,expires_at=?,updated_at=? WHERE id=? AND status='PENDING' AND policy_version=?", (status,json.dumps(approved,sort_keys=True) if status=="APPROVED" else None,reason or "管理员批准",expiry,utc_now(),aid,row['policy_version'])).rowcount
            if changed != 1: raise ApiError(409,"application_already_decided","申请已处理")
            if status == "APPROVED" and row["kind"] == "quota_change":
                self.write_quota(row["owner_uid"], approved)
            self.add_notification(row["owner_uid"],"application_decision","申请已批准" if status=="APPROVED" else "申请已拒绝",reason or "管理员批准")
            self.store.audit(ident["uid"],ident["username"],action,"application",aid,{"approved":approved if status=="APPROVED" else None,"reason":reason},status)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK"); raise
        return dict(db.execute("SELECT * FROM applications WHERE id=?",(aid,)).fetchone())

    def write_quota(self, uid: int, quota: Dict[str, int]) -> None:
        user=self.user(uid)
        self.store.db.execute("INSERT INTO quota_overrides(uid,username,gpu_limit,concurrent_gpu_limit,active_job_limit,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET username=excluded.username,gpu_limit=excluded.gpu_limit,concurrent_gpu_limit=excluded.concurrent_gpu_limit,active_job_limit=excluded.active_job_limit,updated_at=excluded.updated_at",(uid,user.pw_name,quota["gpu_limit"],quota["concurrent_gpu_limit"],quota["active_job_limit"],utc_now()))

    def notifications(self, ident: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows = self.store.db.execute("SELECT * FROM notifications WHERE recipient_uid=? ORDER BY created_at DESC LIMIT 200", (ident["uid"],)).fetchall()
        return [dict(r) for r in rows]

    def read_notification(self, ident: Dict[str, Any], nid: str) -> None:
        self.store.db.execute("UPDATE notifications SET read_at=? WHERE id=? AND recipient_uid=?", (utc_now(), nid, ident["uid"]))

    def rule_preview(self, ident: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        self.require_admin(ident)
        proposed = payload.get("rules")
        if not isinstance(proposed, dict):
            raise ApiError(400, "invalid_rules", "规则必须是对象")
        for name in ("debug", "normal", "long"):
            if name not in proposed or not 1 <= int(proposed[name].get("max_minutes", 0)) <= int(self.config.get("max_job_minutes",4320)):
                raise ApiError(400, "invalid_rules", "每个档位必须有正的单作业最大分钟数")
        impacts = []
        for row in self.store.db.execute("SELECT * FROM jobs WHERE state IN ('WAITING_QUOTA','SUBMITTING','PENDING','RUNNING','COMPLETING','SUSPENDED') ORDER BY created_at").fetchall():
            limit = int(proposed[row["qos"]]["max_minutes"]) if row["qos"] in proposed else 0
            reasons = []
            quota = self.quota(row["owner_uid"])
            if row["requested_gpu"] > quota["gpu_limit"]: reasons.append("GPU 请求超过用户当前个人上限")
            if row['policy_version'] == 1 and row["requested_minutes"] > limit: reasons.append("单作业时限超过新档位上限")
            usage = self.active_usage(row["owner_uid"])
            if usage["active_jobs"] > quota["active_job_limit"]: reasons.append("活动任务数超过用户当前上限")
            if reasons: impacts.append({"job_id": row["id"], "scheduler_id": row["scheduler_id"], "user": row["owner_name"], "reasons": reasons, "action": "仅提示，不自动终止"})
        pid = uuid.uuid4().hex
        current = {"qos": self.qos}
        self.store.db.execute("INSERT INTO rule_previews(id,actor_uid,snapshot_json,proposed_json,impacts_json,created_at) VALUES(?,?,?,?,?,?)", (pid, ident["uid"], json.dumps(current), json.dumps(proposed), json.dumps(impacts), utc_now()))
        return {"preview_id": pid, "before": current, "after": proposed, "impacts": impacts, "created_at": utc_now()}

    def apply_rules(self, ident: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        self.require_admin(ident)
        row = self.store.db.execute("SELECT * FROM rule_previews WHERE id=? AND actor_uid=? AND used_at IS NULL", (payload.get("preview_id"), ident["uid"])).fetchone()
        if not row:
            raise ApiError(409, "preview_required", "规则必须先预览且只能应用一次")
        if parse_iso(row["created_at"]) < dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10) or json.dumps(self.qos, sort_keys=True) != json.dumps(json_object(row["snapshot_json"], {}).get("qos"), sort_keys=True):
            raise ApiError(409, "preview_stale", "规则预览已过期或当前规则已变化，请重新预览")
        proposed = json_object(row["proposed_json"], {})
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            self.store.set_setting("qos", proposed)
            self.store.db.execute("UPDATE rule_previews SET used_at=? WHERE id=?", (utc_now(), row["id"]))
            self.store.audit(ident["uid"], ident["username"], "apply", "rules", row["id"], {"impacts": json_object(row["impacts_json"], [])}, "accepted")
            self.store.db.execute("COMMIT")
        except Exception:
            self.store.db.execute("ROLLBACK")
            raise
        self.qos = proposed
        return {"applied": True, "impacts": json_object(row["impacts_json"], [])}

    def quota_preview(self, ident: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        self.require_admin(ident)
        scope=payload.get("scope","user")
        if scope not in ("default","user"): raise ApiError(400,"invalid_scope","范围无效")
        target=None if scope=="default" else int(payload.get("uid",-1))
        if target is not None: self.identity(target)
        inherit=bool(payload.get("inherit")) and scope=="user"
        proposed={"inherit":True} if inherit else self.quota_value(payload.get("quota"))
        effective=self.default_quota if inherit else proposed
        before=self.default_quota if scope=="default" else self.quota(target)
        impacts=[]
        for row in self.store.db.execute("SELECT * FROM jobs WHERE state IN ('WAITING_QUOTA','SUBMITTING','PENDING','RUNNING','COMPLETING','SUSPENDED')").fetchall():
            if scope=="user" and row["owner_uid"]!=target: continue
            if scope=="default" and self.quota(row["owner_uid"])["source"]!="default": continue
            usage=self.active_usage(row["owner_uid"]); reasons=[]
            if row["requested_gpu"]>effective["gpu_limit"]: reasons.append("单任务GPU超过新额度")
            if usage["gpu"]>effective["gpu_limit"]: reasons.append("保留GPU总量超过新额度")
            if usage["concurrent_jobs"]>effective["concurrent_gpu_limit"]: reasons.append("并发任务数超过新额度")
            if usage["active_jobs"]>effective["active_job_limit"]: reasons.append("活动任务数超过新额度")
            if reasons: impacts.append({"job_id":row["id"],"scheduler_id":row["scheduler_id"],"user":row["owner_name"],"reasons":reasons,"action":"不终止已提交任务；后续任务按新额度排队"})
        pid=uuid.uuid4().hex
        self.store.db.execute("INSERT INTO quota_previews(id,actor_uid,target_uid,target_scope,snapshot_json,proposed_json,impacts_json,created_at) VALUES(?,?,?,?,?,?,?,?)",(pid,ident["uid"],target,scope,json.dumps(before,sort_keys=True),json.dumps(proposed),json.dumps(impacts),utc_now()))
        return {"preview_id":pid,"scope":scope,"target_uid":target,"before":before,"after":effective,"inherit":inherit,"impacts":impacts,"created_at":utc_now()}

    def apply_quota(self, ident: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        self.require_admin(ident)
        row=self.store.db.execute("SELECT * FROM quota_previews WHERE id=? AND actor_uid=? AND used_at IS NULL",(payload.get("preview_id"),ident["uid"])).fetchone()
        if not row or parse_iso(row["created_at"])<dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=10): raise ApiError(409,"preview_stale","预览不存在、已过期或已使用")
        current=self.default_quota if row["target_scope"]=="default" else self.quota(row["target_uid"])
        if current!=json_object(row["snapshot_json"]): raise ApiError(409,"preview_stale","额度已变化，请重新预览")
        proposed=json_object(row["proposed_json"])
        db=self.store.db; db.execute("BEGIN IMMEDIATE")
        try:
            if row["target_scope"]=="default": self.store.set_setting("default_quota",proposed)
            elif proposed.get("inherit"): db.execute("DELETE FROM quota_overrides WHERE uid=?",(row["target_uid"],))
            else: self.write_quota(row["target_uid"],proposed)
            db.execute("UPDATE quota_previews SET used_at=? WHERE id=?",(utc_now(),row["id"]))
            self.store.audit(ident["uid"],ident["username"],"apply","quota",row["id"],{"before":current,"after":proposed},"accepted")
            db.execute("COMMIT")
        except Exception: db.execute("ROLLBACK"); raise
        if row["target_scope"]=="default": self.default_quota=proposed
        return {"applied":True,"impacts":json_object(row["impacts_json"],[])}

    def handle(self, uid: int, request: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(request, dict): raise ApiError(400,"invalid_request","请求必须是对象")
        ident = self.identity(uid)
        method = request.get("method", "GET").upper()
        path = request.get("path", "/api/v1/me")
        payload = request.get("body") or {}
        if not isinstance(payload,dict): raise ApiError(400,"invalid_body","请求正文必须是对象")
        if method == "POST" and path == "/api/v1/priority/users":
            self.require_admin(ident)
            runtime = self.priority_runtime
            if runtime is None:
                raise ApiError(503, "priority_unavailable", "优先级控制尚未就绪")
            try:
                from priority_runtime import PriorityConflict
            except ImportError as exc:
                raise ApiError(503, "priority_unavailable", "优先级控制尚未就绪") from exc
            try:
                if runtime.reconciled_for_current_boot() is not True:
                    raise ControllerUnavailable("priority prerequisites unavailable")
                return runtime.change(ident, payload)
            except PriorityConflict:
                raise ApiError(409, "priority_conflict", "优先级状态、账户身份或待转换任务暂不满足条件，请核对后重试")
            except (ValueError, KeyError, TypeError):
                raise ApiError(400, "invalid_priority_request", "优先级请求无效")
            except Exception:
                raise ApiError(503, "priority_unavailable", "优先级控制尚未就绪")
        if method == "GET" and path == "/api/v1/me": return {"identity": ident, "version": VERSION}
        if method == "GET" and path == "/api/v1/capabilities":
            monthly = self.monthly_controller_snapshot(uid)
            ready = monthly.get("enforcement_ready") is True and monthly.get("selectable_tiers_ready") is True
            return {"schema_version":"1.0", "api_version":VERSION, "identity_source":"unix_peer_uid",
                    "execution":"shell_command", "logs":{"mode":"bounded_tail","follow":False},
                    "multi_node":False,
                    "monthly_quota":{"mode":"enforcing" if ready else "unknown",
                                     "enforcement_ready":ready, "accounting":monthly.get("accounting_status","unknown"),
                                     "selected_tier_required":ready},
                    "monthly_requests":{"available":ready and monthly.get("request_available") is True,
                                        "idempotency":"owner-request-id-v1",
                                        "reconcile":"application-grant-v1"}}
        if method == "GET" and path == "/api/v1/state":
            state = {"identity": ident, "hostname": socket.gethostname(), "display_name": self.config.get("display_name") or socket.gethostname(), "jobs": self.jobs(ident), "monthly_quota": self.monthly_controller_snapshot(uid), "monthly_accounts": [{"uid":a["uid"],"username":a["username"],**a["monthly_quota"]} for a in self.accounts(ident)] if ident["is_admin"] else [], "gpus": self.gpu_inventory(ident), "metrics": self.metrics(ident), "quota": self.quota(uid), "usage": self.active_usage(uid), "accounts": self.accounts(ident), "default_quota": {**self.default_quota,"concurrent_job_limit":self.default_quota["concurrent_gpu_limit"]}, "temporary_authorizations_enabled": bool(self.config.get("temporary_authorizations", {}).get("enabled", False)), "applications": self.applications(ident), "notifications": self.notifications(ident), "rules": self.qos, "scheduler_available": self.last_reconcile_ok, "scheduler_error": self.scheduler_error, "observed_at": utc_now()}
            if ident["is_admin"]:
                capability = {"available": False, "reconciled": False}
                users = {}
                if self.priority_runtime is not None:
                    capability, users = self.priority_runtime.presentation(state["accounts"])
                state["priority_management"] = capability
                state["accounts"] = [{**account, **users.get(account["uid"], {})} for account in state["accounts"]]
            return state
        if method == "GET" and path == "/api/v1/jobs": return {"jobs": self.jobs(ident)}
        if method == "POST" and path == "/api/v1/jobs/validate":
            job = self.validate_job(ident, payload)
            return {"job": job, "quota": self.quota(uid), "usage": self.active_usage(uid)}
        if method == "POST" and path == "/api/v1/jobs":
            job = self.submit(ident, payload)
            return {"job": job}
        if method == "POST" and path.startswith("/api/v1/jobs/") and path.endswith("/cancel"): return {"job": self.cancel(ident, path.split("/")[-2])}
        if method == "GET" and path.startswith("/api/v1/jobs/") and path.endswith("/log"): return self.log(ident, path.split("/")[-2])
        if method == "GET" and path == "/api/v1/applications": return {"applications": self.applications(ident)}
        if method == "POST" and path == "/api/v1/applications":
            application = self.create_application(ident, payload)
            return {"application": application}
        if method == "POST" and path.startswith("/api/v1/applications/") and path.endswith("/grant-reconcile"): return {"application": self.finish_monthly_grant(ident, path.split("/")[-2])}
        if method == "POST" and path.startswith("/api/v1/applications/") and path.endswith("/decision"): return {"application": self.decide_application(ident, path.split("/")[-2], payload)}
        if method == "POST" and path.startswith("/api/v1/notifications/") and path.endswith("/read"): self.read_notification(ident, path.split("/")[-2]); return {"ok": True}
        if method == "POST" and path == "/api/v1/rules/preview": return self.rule_preview(ident, payload)
        if method == "POST" and path == "/api/v1/rules/apply": return self.apply_rules(ident, payload)
        if method == "POST" and path == "/api/v1/quotas/preview": return self.quota_preview(ident, payload)
        if method == "POST" and path == "/api/v1/quotas/apply": return self.apply_quota(ident, payload)
        if method == "GET" and path == "/api/v1/audit":
            self.require_admin(ident)
            rows = self.store.db.execute("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 500").fetchall()
            return {"audit": [dict(r) for r in rows]}
        raise ApiError(404, "not_found", "未知 API 路径")


def shell_quote(value: str) -> str:
    # Used only inside the generated user-owned script, never for daemon execution.
    return "'" + value.replace("'", "'\\''") + "'"


def serve(service: Service) -> None:
    path=pathlib.Path(service.socket_path)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
    try: path.unlink()
    except FileNotFoundError: pass
    sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    sock.bind(str(path)); os.chmod(path,0o660)
    os.chown(path,0,grp.getgrnam(service.config.get("socket_group",service.training_group)).gr_gid)
    # Existing Cockpit sessions may predate group enrollment. Grant only the
    # already-authorized admin UIDs; identity is still checked on every request.
    for uid in service.admin_uids():
        code,_,err=run(["/usr/bin/setfacl","-m","u:%d:rw" % uid,str(path)])
        if code: print("socket admin ACL unavailable: %s" % err,file=sys.stderr)
    sock.listen(64); sock.settimeout(2.0)
    while True:
        try: service.tick()
        except Exception as exc: print("scheduler tick: %s" % exc,file=sys.stderr,flush=True)
        try: conn,_=sock.accept()
        except socket.timeout: continue
        with conn:
            uid=-1
            try:
                conn.settimeout(3.0); uid=service.peer_uid(conn)
                service.identity(uid)
                raw=bytearray(); deadline=time.monotonic()+3
                while b"\n" not in raw:
                    if time.monotonic()>deadline: raise ApiError(408,"request_timeout","请求接收超时")
                    block=conn.recv(min(65536,1048577-len(raw)))
                    if not block: break
                    raw.extend(block)
                    if len(raw)>1048576: raise ApiError(413,"request_too_large","请求超过1MiB")
                req=json.loads(bytes(raw).split(b"\n",1)[0].decode("utf-8"))
                result={"ok":True,"data":service.handle(uid,req)}
            except ApiError as exc:
                result={"ok":False,"error":{"status":exc.status,"code":exc.code,"message":exc.message,"details":exc.details}}
            except (ValueError,TypeError,KeyError) as exc:
                result={"ok":False,"error":{"status":400,"code":"invalid_request","message":"请求字段或格式无效"}}
            except Exception as exc:
                print("API error uid=%s: %s" % (uid,exc),file=sys.stderr,flush=True)
                result={"ok":False,"error":{"status":500,"code":"internal_error","message":"后端处理失败"}}
            try: conn.sendall((json.dumps(result,ensure_ascii=False)+"\n").encode())
            except OSError: pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args()
    try:
        serve(Service(args.config))
    except Exception as exc:
        print("pro6000d-training-api: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
