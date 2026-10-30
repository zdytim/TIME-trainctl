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
import base64
import datetime as dt
import grp
import hashlib
import json
import os
import pathlib
import pwd
import re
import shutil
import selectors
import socket
import struct
import stat
from contextlib import contextmanager
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
from collections import deque
from typing import Any, Dict, Iterable, List, Optional, Tuple


VERSION = "0.1.0"
DEFAULT_CONFIG = "/etc/pro6000d-training/config.json"
DEFAULT_SOCKET = "/run/pro6000d-training/backend.sock"
ACTIVE_STATES = ("WAITING_QUOTA", "SUBMITTING", "PENDING", "RUNNING", "COMPLETING", "SUSPENDED")
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


class Store:
    def __init__(self, path: str):
        self.path = path
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
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
        self.store.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_request ON jobs(owner_uid,client_request_id) WHERE client_request_id IS NOT NULL")

        self.monthly = MonthlyLedger(self)

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
            users.append({**who, "quota": self.quota(user.pw_uid), "usage": self.active_usage(user.pw_uid), "monthly_quota": self.monthly.summary(user.pw_uid)})
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

    def validate_job(self, ident: Dict[str, Any], payload: Dict[str, Any], temporary: bool = False) -> Dict[str, Any]:
        user = self.user(ident["uid"])
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_job", "任务必须是对象")
        name = payload.get("name", "training")
        command = payload.get("command", "")
        qos = payload.get("qos", "normal")
        try:
            gpu = int(payload.get("gpu", 1)); cpu = int(payload.get("cpu", 1))
            mem = int(payload.get("memory_mb", 1024)); minutes = int(payload.get("time_minutes", 60))
        except (TypeError, ValueError):
            raise ApiError(400, "invalid_resource", "GPU、CPU、内存和时限必须是整数")
        if not SAFE_NAME.fullmatch(str(name)):
            raise ApiError(400, "invalid_name", "任务名只允许字母、数字、点、下划线和连字符")
        if not isinstance(command, str) or not command.strip() or len(command) > 4096 or "\x00" in command:
            raise ApiError(400, "invalid_command", "命令不能为空、不能超过 4096 字符或包含 NUL")
        if qos not in self.qos:
            raise ApiError(400, "invalid_qos", "未知时限档位")
        if gpu < 1 or gpu > int(self.config.get("max_requested_gpu", 64)) or cpu < 1 or cpu > 512 or mem < 128 or mem > 1048576:
            raise ApiError(400, "invalid_resource", "资源请求超出接口允许范围")
        if minutes < 1 or minutes > int(self.config.get("max_job_minutes", 4320)):
            raise ApiError(400, "cluster_time_limit", "时限超出测试节点允许的最大值")
        if not temporary and minutes > int(self.qos[qos]["max_minutes"]):
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
                "output_path": output, "environment": env_name}

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
        path = pathlib.Path(self.config.get("admission_plugin_path", "/etc/slurm-llnl/job_submit.lua"))
        try:
            st = path.stat()
            if st.st_uid != 0 or st.st_mode & 0o022 or path.is_symlink():
                return False
            if "PRO6000D_ROOT_BROKER_V1" not in path.read_text():
                return False
            code, out, _ = run(["scontrol", "show", "config"], timeout=5)
            return code == 0 and bool(re.search(r"JobSubmitPlugins\s*=\s*[^\n]*\blua\b", out))
        except OSError:
            return False

    def reconcile(self) -> None:
        self.last_reconcile_ok = False
        code, out, err = run(["scontrol", "show", "jobs", "-o"], timeout=8)
        if code:
            self.scheduler_error = err or "调度器不可达"
            return
        self.scheduler_error = None
        self.last_reconcile_ok = True
        records = {}
        for line in out.splitlines():
            def field(key):
                match = re.search(r"(?:^| )" + key + r"=([^ ]+)", line)
                return match.group(1) if match else ""
            sid = field("JobId")
            if sid.isdigit():
                records[sid] = {"state": field("JobState"), "reason": field("Reason"), "name": field("JobName"), "comment": field("Comment"), "line": line}
        complete = self.completion_records()
        rows = self.store.db.execute("SELECT * FROM jobs WHERE state IN ('SUBMITTING','PENDING','RUNNING','COMPLETING','SUSPENDED')").fetchall()
        for row in rows:
            sid = row["scheduler_id"]
            if not sid:
                candidates = [key for key, value in records.items() if value["name"] == "p6-" + row["id"]]
                if not candidates:
                    candidates = [key for key, value in complete.items() if value.get("name") == "p6-" + row["id"]]
                if len(candidates) == 1:
                    sid = candidates[0]
                    self.store.db.execute("UPDATE jobs SET scheduler_id=? WHERE id=?", (sid, row["id"]))
                else:
                    continue  # Ambiguous submit never auto-retries; avoids duplicate jobs.
            rec = records.get(sid) or complete.get(sid)
            if rec and rec.get("name") != "p6-" + row["id"]:
                continue
            if not rec:
                self.store.db.execute("UPDATE jobs SET reason_code='completion_unknown',reason_text='调度器已无记录，等待管理员核对；保留额度，避免重复提交',updated_at=? WHERE id=?", (utc_now(), row["id"]))
                continue
            state = rec["state"].split("+")[0]
            if not state:
                continue
            reason = rec.get("reason", "")
            final = state not in ("PENDING", "RUNNING", "COMPLETING", "SUSPENDED", "CONFIGURING", "RESIZING", "REQUEUED", "REQUEUE_FED")
            if state in ("CONFIGURING", "RESIZING", "REQUEUED", "REQUEUE_FED"):
                state = "PENDING"
            self.store.db.execute("UPDATE jobs SET state=?,reason_code=?,reason_text=?,updated_at=?,started_at=COALESCE(started_at,?),finished_at=? WHERE id=?", (state, reason, reason or state, utc_now(), utc_now() if state == "RUNNING" else None, utc_now() if final else None, row["id"]))
            if final and row["state"] != state:
                self.add_notification(row["owner_uid"], "job_finished", "任务已结束", "%s：%s" % (row["name"], state))

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
        if time.monotonic() - self.last_tick < 3:
            return
        self.last_tick = time.monotonic()
        self.reconcile()
        self.monthly.refresh()
        self.expire_applications()
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

    def gpu_process_identity(self, pid: int) -> Dict[str, Any]:
        """Never read cmdline/environment; verify UID and process start time twice."""
        base = pathlib.Path("/proc") / str(pid)
        unknown = {"uid":None,"username":None,"name":"未知进程","job_id":None,
                   "job_name":None,"scheduler_id":None,"association":"unavailable"}
        try:
            def snapshot():
                uid = base.stat().st_uid
                with (base / "stat").open() as stream:
                    value = stream.read(8192)
                tail = value[value.rfind(")")+2:].split()
                return uid, tail[19]
            before = snapshot()
            try:
                name = os.path.basename(os.readlink(str(base / "exe")))[:200]
            except OSError:
                with (base / "comm").open() as stream:
                    name = stream.read(256).strip()[:200]
            with (base / "cgroup").open() as stream:
                groups = stream.read(32768)
            if snapshot() != before:
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
            if len(job_ids) == 1:
                sid = next(iter(job_ids))
                row = self.store.db.execute("SELECT id,name,scheduler_id FROM jobs WHERE scheduler_id=? AND owner_uid=? ORDER BY created_at DESC LIMIT 1",(sid,uid)).fetchone()
                if row:
                    result.update(job_id=row["id"],job_name=row["name"],scheduler_id=row["scheduler_id"],association="matched")
            return result
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
                    if pid not in identities: identities[pid] = self.gpu_process_identity(pid)
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
        return {"name": row["name"], "qos": row["qos"], "gpu": row["requested_gpu"],
                "cpu": row["requested_cpu"], "memory_mb": row["requested_mem_mb"],
                "time_minutes": row["requested_minutes"], "workdir": row["workdir"],
                "environment": row["env_name"], "command": row["command"], "output_path": row["output_path"]}

    def dispatch_job(self, ident: Dict[str, Any], row: sqlite3.Row, job: Dict[str, Any], temporary: bool = False) -> Dict[str, Any]:
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
        # Commit the submission intent before the external side effect. A crash
        # or timeout leaves SUBMITTING and is reconciled by unique scheduler name.
        self.store.db.execute("UPDATE jobs SET state='SUBMITTING',reason_code='submitting',reason_text='正在提交调度器',updated_at=? WHERE id=?", (utc_now(), row["id"]))
        # ALL exports only run()'s explicit sanitized environment. On Slurm 19,
        # NONE implicitly asks Slurm to collect a login environment through PAM.
        argv = ["sbatch", "--parsable", "--uid", str(user.pw_uid), "--gid", str(job_gid),
                "--job-name", "p6-" + row["id"], "--partition", self.config.get("partition", "gpu"),
                "--comment", "pro6000d-job:" + row["id"], "--gres", "gpu:%d" % job["gpu"],
                "--cpus-per-task", str(job["cpu"]), "--mem", "%dM" % job["memory_mb"],
                "--time", str(job["time_minutes"]), "--chdir", job["workdir"],
                "--export", "ALL", "--output", job["output_path"], "--error", job["output_path"], str(script)]
        code, out, err = run(argv, timeout=20)
        sid = out.split(";", 1)[0].strip()
        if code == 0 and sid.isdigit():
            self.store.db.execute("UPDATE jobs SET state='PENDING',scheduler_id=?,reason_code='waiting_for_scheduler',reason_text='已提交 Slurm，等待资源',updated_at=? WHERE id=?", (sid, utc_now(), row["id"]))
        elif code == 124 or code == 0:
            self.store.db.execute("UPDATE jobs SET reason_code='submission_uncertain',reason_text='提交结果未确认；保留请求和额度，后台核对，不重复提交',updated_at=? WHERE id=?", (utc_now(), row["id"]))
        else:
            self.store.db.execute("UPDATE jobs SET state='REJECTED',reason_code='scheduler_rejected',reason_text=?,updated_at=?,finished_at=? WHERE id=?", (err[:2000] or "调度器拒绝", utc_now(), utc_now(), row["id"]))
        self.store.audit(user.pw_uid, user.pw_name, "dispatch", "job", row["id"], {"scheduler_id": sid if sid.isdigit() else None, "returncode": code}, "accepted" if code == 0 else "uncertain" if code == 124 else "rejected")
        return self.job_json(self.store.db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def dispatch_waiting(self) -> None:
        if not self.last_reconcile_ok or not self.admission_available():
            return
        for row in self.store.db.execute("SELECT * FROM jobs WHERE state='WAITING_QUOTA' ORDER BY created_at,id LIMIT 256").fetchall():
            try:
                ident = self.identity(row["owner_uid"])
                monthly_block = self.monthly.block_reason(row["owner_uid"])
                if monthly_block:
                    self.store.db.execute("UPDATE jobs SET reason_code='monthly_quota_wait',reason_text=?,updated_at=? WHERE id=?", (monthly_block,utc_now(),row["id"]))
                    continue
                quota = self.quota(row["owner_uid"])
                usage = self.active_usage(row["owner_uid"])
                job = self.row_job(row)
                app = self.store.db.execute("SELECT * FROM applications WHERE id=?", (row["temporary_application_id"],)).fetchone() if row["temporary_application_id"] else None
                temporary = bool(app and app["status"] == "APPROVED" and app["bound_job_id"] == row["id"])
                if app and (not temporary or parse_iso(app["expires_at"]) <= dt.datetime.now(dt.timezone.utc)):
                    self.store.db.execute("UPDATE jobs SET state='EXPIRED',reason_code='approval_expired',reason_text='批准在运行前过期，需重新申请',finished_at=?,updated_at=? WHERE id=?", (utc_now(), utc_now(), row["id"]))
                    continue
                cap = max(quota["gpu_limit"], job["gpu"]) if temporary else quota["gpu_limit"]
                if not temporary and (job["gpu"] > quota["gpu_limit"] or job["time_minutes"] > int(self.qos[job["qos"]]["max_minutes"])):
                    self.store.db.execute("UPDATE jobs SET reason_code='policy_changed',reason_text='规则已变更，任务超过当前额度或档位时限；请取消后重新申请' WHERE id=?", (row["id"],))
                    continue
                if usage["gpu"] + job["gpu"] > cap or usage["concurrent_jobs"] >= quota["concurrent_job_limit"]:
                    continue
                self.validate_job(ident, job, temporary=temporary)
                self.dispatch_job(ident, row, job, temporary)
            except (ApiError, KeyError, OSError) as exc:
                self.store.db.execute("UPDATE jobs SET reason_code='dispatch_blocked',reason_text=?,updated_at=? WHERE id=? AND state='WAITING_QUOTA'", (str(exc)[:1000], utc_now(), row["id"]))

    def submit(self, ident: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        uid = ident["uid"]
        request_id = payload.get("client_request_id")
        if request_id is not None:
            if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,80}", request_id):
                raise ApiError(400, "invalid_request_id", "请求标识无效")
            prior = self.store.db.execute("SELECT * FROM jobs WHERE owner_uid=? AND client_request_id=?", (uid, request_id)).fetchone()
            if prior:
                return self.job_json(prior)
        self.tick()
        if not self.scheduler_available() or not self.admission_available():
            raise ApiError(503, "scheduler_unavailable", "调度器或受控提交插件尚未就绪")
        app_id = payload.get("temporary_application_id")
        app = None
        if app_id:
            app = self.store.db.execute("SELECT * FROM applications WHERE id=? AND owner_uid=?", (str(app_id), uid)).fetchone()
            if app and app["bound_job_id"]:
                prior = self.store.db.execute("SELECT * FROM jobs WHERE id=?", (app["bound_job_id"],)).fetchone()
                if prior:
                    return self.job_json(prior)
            if not app or app["kind"] != "temporary_job" or app["status"] != "APPROVED" or app["authorization_used"]:
                raise ApiError(409, "invalid_temporary_authorization", "批准不存在、未批准或已使用")
            if not app["expires_at"] or parse_iso(app["expires_at"]) <= dt.datetime.now(dt.timezone.utc):
                raise ApiError(409, "temporary_authorization_expired", "批准已过期")
            approved = json_object(app["approved_json"], {})
            job = self.validate_job(ident, approved, temporary=True)
            supplied = {key: payload[key] for key in job if key in payload}
            if supplied and any(supplied[key] != job[key] for key in supplied):
                raise ApiError(409, "authorization_binding_mismatch", "批准绑定的任务已变更，必须重新申请")
        else:
            job = self.validate_job(ident, payload)
            if job["gpu"] > self.quota(uid)["gpu_limit"]:
                raise ApiError(409, "permanent_quota_exceeded", "单任务 GPU 请求超过个人上限，请申请临时资源")
        usage = self.active_usage(uid)
        if usage["active_jobs"] >= self.quota(uid)["active_job_limit"]:
            raise ApiError(409, "active_job_limit", "活动任务数量已满，包括运行和排队任务；请先取消或等待任务结束")
        jid = uuid.uuid4().hex
        db = self.store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            if app:
                changed = db.execute("UPDATE applications SET authorization_used=1,bound_job_id=?,updated_at=? WHERE id=? AND status='APPROVED' AND authorization_used=0 AND bound_job_id IS NULL", (jid, utc_now(), app_id)).rowcount
                if changed != 1:
                    raise ApiError(409, "authorization_consumed", "批准已被其他请求使用")
            db.execute("INSERT INTO jobs(id,owner_uid,owner_name,name,state,requested_gpu,requested_cpu,requested_mem_mb,requested_minutes,qos,workdir,env_name,command,output_path,reason_code,reason_text,temporary_application_id,created_at,updated_at,client_request_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (jid, uid, ident["username"], job["name"], "WAITING_QUOTA", job["gpu"], job["cpu"], job["memory_mb"], job["time_minutes"], job["qos"], job["workdir"], job["environment"], job["command"], job["output_path"], "waiting_user_quota", "已入队，等待个人额度与设备资源", app_id, utc_now(), utc_now(), request_id))
            self.store.audit(uid, ident["username"], "queue", "job", jid, {"temporary_application_id": app_id}, "accepted")
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
        return {**{k: row[k] for k in row.keys()}, "usage_gpu_seconds": self.monthly.job_usage(row)}

    def jobs(self, ident: Dict[str, Any]) -> List[Dict[str, Any]]:
        self.tick()
        if ident["is_admin"]:
            rows = self.store.db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1000").fetchall()
        else:
            rows = self.store.db.execute("SELECT * FROM jobs WHERE owner_uid=? ORDER BY created_at DESC LIMIT 1000", (ident["uid"],)).fetchall()
        return [self.job_json(x) for x in rows]

    def cancel(self, ident: Dict[str, Any], jid: str) -> Dict[str, Any]:
        self.tick()
        row = self.store.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            raise ApiError(404, "job_not_found", "任务不存在")
        if row["owner_uid"] != ident["uid"] and not ident["is_admin"]:
            raise ApiError(403, "job_forbidden", "只能操作自己的任务")
        if row["state"] not in ACTIVE_STATES:
            return self.job_json(row)
        if row["state"] == "SUBMITTING" and not row["scheduler_id"]:
            raise ApiError(409, "submission_uncertain", "提交结果尚未确认，不能假报取消成功；请稍后核对")
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
        now = dt.datetime.now(dt.timezone.utc)
        for row in self.store.db.execute("SELECT * FROM applications WHERE kind='temporary_job' AND status='APPROVED' AND expires_at IS NOT NULL").fetchall():
            if parse_iso(row["expires_at"]) > now:
                continue
            job = self.store.db.execute("SELECT * FROM jobs WHERE id=?", (row["bound_job_id"],)).fetchone() if row["bound_job_id"] else None
            # Expiry is admission deadline. Once sent to Slurm it is grandfathered;
            # never kill a running or scheduler-pending job on approval expiry.
            if job and job["state"] != "WAITING_QUOTA":
                continue
            self.store.db.execute("UPDATE applications SET status='EXPIRED',updated_at=? WHERE id=?", (utc_now(), row["id"]))
            if job:
                self.store.db.execute("UPDATE jobs SET state='EXPIRED',reason_code='approval_expired',reason_text='批准在提交调度器前已过期',finished_at=?,updated_at=? WHERE id=?", (utc_now(), utc_now(), job["id"]))
            self.add_notification(row["owner_uid"], "application_expired", "批准已过期", "未提交给调度器的授权已失效；已提交的任务不受影响。")

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
        if kind == "monthly_gpu_hours":
            if not self.monthly.summary(ident["uid"])["enforcement_ready"]:
                raise ApiError(409,"monthly_not_enabled","月度启动限制尚未启用，暂不受理追加申请")
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
            requested = self.validate_job(ident, requested, temporary=True)
        else:
            requested = self.quota_value(requested)
        if self.store.db.execute("SELECT COUNT(*) FROM applications WHERE owner_uid=? AND status='PENDING'", (ident["uid"],)).fetchone()[0] >= 32:
            raise ApiError(409, "pending_application_limit", "待审批申请已达 32 项")
        aid = uuid.uuid4().hex
        db=self.store.db; db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("INSERT INTO applications(id,owner_uid,owner_name,kind,status,requested_json,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (aid, ident["uid"], ident["username"], kind, "PENDING", json.dumps(requested, sort_keys=True), reason, utc_now(), utc_now()))
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
        if action != "reject":
            adjusted = payload.get("approved", {}) if action == "approve_adjusted" else {}
            if not isinstance(adjusted, dict):
                raise ApiError(400, "invalid_approved", "批准内容必须是对象")
            if row["kind"] == "monthly_gpu_hours":
                if not self.monthly.summary(row["owner_uid"])["enforcement_ready"]:
                    raise ApiError(409,"monthly_not_enabled","月度启动限制尚未启用，暂不能批准追加额度")
                approved = self.monthly_request({**approved, **{k:v for k,v in adjusted.items() if k == "gpu_hours"}})
            elif row["kind"] == "temporary_job":
                for key in ("gpu", "cpu", "memory_mb", "time_minutes", "qos"):
                    if key in adjusted: approved[key] = adjusted[key]
                approved = self.validate_job(self.identity(row["owner_uid"]), approved, temporary=True)
                valid = int(payload.get("valid_minutes", 1440))
                if not 1 <= valid <= 10080:
                    raise ApiError(400, "invalid_expiry", "有效期须为 1 到 10080 分钟")
                expiry=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(minutes=valid)).isoformat()
            else:
                approved=self.quota_value({**approved, **adjusted})
        status="REJECTED" if action == "reject" else "APPROVED"
        db=self.store.db; db.execute("BEGIN IMMEDIATE")
        try:
            changed=db.execute("UPDATE applications SET status=?,approved_json=?,decision_reason=?,expires_at=?,updated_at=? WHERE id=? AND status='PENDING'", (status,json.dumps(approved,sort_keys=True) if status=="APPROVED" else None,reason or "管理员批准",expiry,utc_now(),aid)).rowcount
            if changed != 1: raise ApiError(409,"application_already_decided","申请已处理")
            if status == "APPROVED" and row["kind"] == "monthly_gpu_hours":
                db.execute("INSERT INTO monthly_additions VALUES(?,?,?,?,?,?,?)", (aid,row["owner_uid"],approved["month"],approved["gpu_hours"]*3600,ident["uid"],reason or "管理员批准",utc_now()))
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
            if row["requested_minutes"] > limit: reasons.append("单作业时限超过新档位上限")
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
        if method == "GET" and path == "/api/v1/me": return {"identity": ident, "version": VERSION}
        if method == "GET" and path == "/api/v1/capabilities":
            return {"schema_version": "1.0", "api_version": VERSION,
                    "identity_source": "unix_peer_uid", "execution": "shell_command",
                    "logs": {"mode": "bounded_tail", "follow": False},
                    "multi_node": "unknown",
                    "monthly_quota": {"accounting": self.monthly.status,
                                      "mode": "staged" if self.monthly.enabled else "disabled",
                                      "enforcement_ready": False}}
        if method == "GET" and path == "/api/v1/state":
            return {"identity": ident, "hostname": socket.gethostname(), "jobs": self.jobs(ident), "monthly_quota": self.monthly.summary(uid), "monthly_accounts": [{"uid":a["uid"],"username":a["username"],**a["monthly_quota"]} for a in self.accounts(ident)] if ident["is_admin"] else [], "gpus": self.gpu_inventory(ident), "metrics": self.metrics(ident), "quota": self.quota(uid), "accounts": self.accounts(ident), "default_quota": {**self.default_quota,"concurrent_job_limit":self.default_quota["concurrent_gpu_limit"]}, "temporary_authorizations_enabled": bool(self.config.get("temporary_authorizations", {}).get("enabled", False)), "applications": self.applications(ident), "notifications": self.notifications(ident), "rules": self.qos, "scheduler_available": self.last_reconcile_ok, "scheduler_error": self.scheduler_error, "observed_at": utc_now()}
        if method == "GET" and path == "/api/v1/jobs": return {"jobs": self.jobs(ident)}
        if method == "POST" and path == "/api/v1/jobs/validate": return {"job": self.validate_job(ident, payload), "quota": self.quota(uid), "usage": self.active_usage(uid)}
        if method == "POST" and path == "/api/v1/jobs": return {"job": self.submit(ident, payload)}
        if method == "POST" and path.startswith("/api/v1/jobs/") and path.endswith("/cancel"): return {"job": self.cancel(ident, path.split("/")[-2])}
        if method == "GET" and path.startswith("/api/v1/jobs/") and path.endswith("/log"): return self.log(ident, path.split("/")[-2])
        if method == "GET" and path == "/api/v1/applications": return {"applications": self.applications(ident)}
        if method == "POST" and path == "/api/v1/applications": return {"application": self.create_application(ident, payload)}
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
