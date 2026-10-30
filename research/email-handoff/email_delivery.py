"""Safe, opt-in email delivery for application notifications.

The application API owns the transactional outbox write.  This module owns
configuration validation and asynchronous delivery.  It deliberately keeps
the SMTP side free of application credentials and training payloads: callers
pass a pre-rendered, allow-listed summary only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import smtplib
import socket
import sqlite3
import ssl
import stat
import threading
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


DEFAULT_BACKOFF_SECONDS = (60, 300, 1800, 7200, 21600, 86400)
DEFAULT_POLL_SECONDS = 5
DEFAULT_SEND_TIMEOUT_SECONDS = 15
DEFAULT_MAX_ATTEMPTS = 6
LOCK_GRACE_SECONDS = 120
MAX_BODY_LENGTH = 12000
MAX_ERROR_LENGTH = 240
EMAIL_CONFIG_NAME = "email_notifications"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_after(seconds: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=max(0, int(seconds)))).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean_text(value: Any, limit: int = 160) -> str:
    """Return display text without control characters or header newlines."""
    text = str(value if value is not None else "")
    text = "".join(ch if ord(ch) >= 32 and ch not in "\x7f\r\n" else " " for ch in text)
    return " ".join(text.split())[:limit]


def _config(config: Dict[str, Any]) -> Dict[str, Any]:
    value = config.get(EMAIL_CONFIG_NAME, {}) if isinstance(config, dict) else {}
    return value if isinstance(value, dict) else {}


def _valid_email(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
        return None
    _, address = parseaddr(value)
    if not address or "\r" in address or "\n" in address or " " in address:
        return None
    # This is intentionally a conservative syntax check; DNS delivery is the
    # SMTP administrator's responsibility and is never performed here.
    if address.count("@") != 1 or address.startswith("@") or address.endswith("@"):
        return None
    local, domain = address.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        return None
    return value.strip()


def normalize_recipients(value: Any) -> List[str]:
    if isinstance(value, str):
        values: Iterable[Any] = [value]
    elif isinstance(value, list):
        values = value
    else:
        return []
    result: List[str] = []
    for item in values:
        address = _valid_email(item)
        if address and address not in result:
            result.append(address)
    return result[:100]


def _external_host_is_local(host: str) -> bool:
    lowered = host.rstrip(".").lower()
    if lowered in {"localhost", "localhost.localdomain", "ip6-localhost"} or lowered.endswith(".localhost") or lowered.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return bool(address.is_loopback or address.is_private or address.is_link_local or address.is_unspecified or address.is_reserved or address.is_multicast)


def validate_external_page_url(value: Any) -> str:
    """Validate the URL printed in mail; never treat localhost as external."""
    if not isinstance(value, str) or not value.strip() or "\r" in value or "\n" in value:
        raise ValueError("approval_page_url_required")
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        raise ValueError("approval_page_url_invalid")
    if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("approval_page_url_must_be_https")
    try:
        host = parsed.hostname
    except ValueError:
        host = None
    if not host or _external_host_is_local(host):
        raise ValueError("approval_page_url_must_be_external")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("approval_page_url_invalid_port")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("approval_page_url_invalid_port")
    if any(re.search(r"(?:token|secret|password|passwd|auth|api[_-]?key)", key, re.IGNORECASE)
           for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        raise ValueError("approval_page_url_must_not_contain_credentials")
    # Fragments are client-only and can be silently discarded by mail clients;
    # the application id is added as a query parameter below.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def application_page_link(base_url: str, application_id: str) -> str:
    base = validate_external_page_url(base_url)
    parsed = urlsplit(base)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("page", "applications"))
    query.append(("application_id", _clean_text(application_id, 128)))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _int_value(data: Dict[str, Any], key: str) -> Optional[int]:
    try:
        value = data.get(key)
        if isinstance(value, bool) or value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def safe_application_summary(kind: str, data: Any) -> Dict[str, Any]:
    """Allow-list fields suitable for an email or audit detail.

    In particular, temporary-job command, workdir, output path and environment
    are never copied into this structure.
    """
    source = data if isinstance(data, dict) else {}
    if kind == "temporary_job":
        result: Dict[str, Any] = {
            "name": _clean_text(source.get("name", "训练任务"), 64),
            "gpu": _int_value(source, "gpu"),
            "cpu": _int_value(source, "cpu"),
            "memory_mb": _int_value(source, "memory_mb"),
            "time_minutes": _int_value(source, "time_minutes"),
            "qos": _clean_text(source.get("qos", ""), 32),
        }
        return {key: value for key, value in result.items() if value is not None}
    if kind == "monthly_gpu_hours":
        return {
            "month": _clean_text(source.get("month", ""), 16),
            "gpu_hours": _int_value(source, "gpu_hours"),
        }
    if kind == "quota_change":
        concurrent = source.get("concurrent_job_limit", source.get("concurrent_gpu_limit"))
        result = {
            "gpu_limit": _int_value(source, "gpu_limit"),
            "concurrent_job_limit": _int_value({"value": concurrent}, "value"),
            "active_job_limit": _int_value(source, "active_job_limit"),
        }
        return {key: value for key, value in result.items() if value is not None}
    return {}


def summary_text(kind: str, summary: Dict[str, Any]) -> str:
    if kind == "temporary_job":
        return "{} · {} GPU · {} CPU · {} MiB · {} 分钟 · {}".format(
            summary.get("name", "训练任务"), summary.get("gpu", "—"),
            summary.get("cpu", "—"), summary.get("memory_mb", "—"),
            summary.get("time_minutes", "—"), summary.get("qos", "—"))
    if kind == "monthly_gpu_hours":
        return "{} · 追加 {} GPU 小时".format(summary.get("month", "—"), summary.get("gpu_hours", "—"))
    if kind == "quota_change":
        return "{} GPU / {} 个并发任务 / {} 个活动任务".format(
            summary.get("gpu_limit", "—"), summary.get("concurrent_job_limit", "—"),
            summary.get("active_job_limit", "—"))
    return "申请摘要不可用"


def application_kind_text(kind: str) -> str:
    return {
        "temporary_job": "一次性训练资源",
        "monthly_gpu_hours": "本月 GPU 小时追加",
        "quota_change": "长期并发限制",
    }.get(kind, "资源申请")


def _sender(value: Any) -> Optional[str]:
    return _valid_email(value)


def _credential_file_status(path_value: Any) -> Optional[str]:
    if not path_value:
        return None
    if not isinstance(path_value, str) or not os.path.isabs(path_value):
        return "credentials_path_invalid"
    try:
        st = os.lstat(path_value)
    except OSError:
        return "credentials_file_unreadable"
    if st.st_uid != 0 or not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or stat.S_IMODE(st.st_mode) & 0o077:
        return "credentials_file_permissions"
    if st.st_size > 16384:
        return "credentials_file_too_large"
    return None


def email_configuration_status(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return non-secret configuration health; never reads credential contents."""
    settings = _config(config)
    enabled = bool(settings.get("enabled", False))
    result: Dict[str, Any] = {
        "enabled": enabled,
        "configured": False,
        "error_code": None,
        "warnings": [],
    }
    if not enabled:
        return result
    smtp = settings.get("smtp", {})
    if not isinstance(smtp, dict):
        result["error_code"] = "smtp_config_invalid"
        return result
    host = smtp.get("host")
    if not isinstance(host, str) or not host.strip() or "\r" in host or "\n" in host:
        result["error_code"] = "smtp_host_required"
        return result
    try:
        port = int(smtp.get("port"))
    except (TypeError, ValueError):
        result["error_code"] = "smtp_port_invalid"
        return result
    if not 1 <= port <= 65535:
        result["error_code"] = "smtp_port_invalid"
        return result
    if smtp.get("tls_mode", "starttls") not in ("none", "starttls", "tls"):
        result["error_code"] = "smtp_tls_mode_invalid"
        return result
    if not _sender(smtp.get("sender")):
        result["error_code"] = "smtp_sender_required"
        return result
    try:
        admin_recipients = normalize_recipients(settings.get("admin_recipients", []))
    except Exception:
        admin_recipients = []
    if not admin_recipients:
        result["error_code"] = "admin_recipients_required"
        return result
    try:
        validate_external_page_url(settings.get("approval_page_url"))
    except ValueError as exc:
        result["error_code"] = str(exc)
        return result
    credential_error = _credential_file_status(smtp.get("credentials_file", ""))
    if credential_error:
        result["error_code"] = credential_error
        return result
    try:
        max_attempts = int(settings.get("max_attempts", DEFAULT_MAX_ATTEMPTS))
    except (TypeError, ValueError):
        result["error_code"] = "max_attempts_invalid"
        return result
    if not 1 <= max_attempts <= 12:
        result["error_code"] = "max_attempts_invalid"
        return result
    try:
        timeout = int(settings.get("send_timeout_seconds", DEFAULT_SEND_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        result["error_code"] = "send_timeout_invalid"
        return result
    if not 3 <= timeout <= 120:
        result["error_code"] = "send_timeout_invalid"
        return result
    backoff = settings.get("backoff_seconds", list(DEFAULT_BACKOFF_SECONDS))
    if not isinstance(backoff, list) or not backoff or any(isinstance(x, bool) or not isinstance(x, (int, float)) or x < 1 for x in backoff):
        result["error_code"] = "backoff_invalid"
        return result
    result["configured"] = True
    mapping = settings.get("user_email_map", {})
    if not isinstance(mapping, dict) or not mapping:
        result["warnings"].append("user_recipient_mapping_empty")
    return result


def _user_recipients(settings: Dict[str, Any], uid: int, username: str) -> List[str]:
    mapping = settings.get("user_email_map", {})
    if not isinstance(mapping, dict):
        return []
    value = mapping.get(username, mapping.get(str(uid), []))
    return normalize_recipients(value)


def _render_application_email(audience: str, phase: str, application_id: str, owner_name: str,
                              kind: str, summary: Dict[str, Any], link: str,
                              outcome: Optional[str] = None) -> Tuple[str, str]:
    short_id = _clean_text(application_id, 12)
    owner = _clean_text(owner_name, 80)
    kind_label = application_kind_text(kind)
    summary_line = summary_text(kind, summary)
    if phase == "created" and audience == "admin":
        subject = "u20 TIME 有新的资源申请 #{}".format(short_id)
        intro = "有新的申请等待管理员处理。"
        footer = "请登录平台后使用既有权限和审批页面处理；此邮件不提供直接审批操作。"
    elif phase == "created":
        subject = "u20 TIME 申请已提交 #{}".format(short_id)
        intro = "你的资源申请已提交，等待管理员处理。"
        footer = "请登录平台查看进度；此邮件仅作通知。"
    else:
        labels = {
            "approved": ("已通过", "申请已按原申请通过。"),
            "approved_adjusted": ("调整后通过", "申请已调整后通过；请以平台中的批准内容为准。"),
            "rejected": ("已拒绝", "申请未获批准；请登录平台查看处理结果。"),
        }
        label, intro = labels.get(outcome or "", ("已处理", "申请已有处理结果。"))
        subject = "u20 TIME 申请{} #{}".format(label, short_id)
        footer = "请登录平台查看完整处理意见；此邮件不提供直接审批操作。"
    lines = [
        "u20 TIME 申请通知",
        "",
        intro,
        "申请编号：{}".format(_clean_text(application_id, 128)),
        "申请人：{}".format(owner),
        "申请类型：{}".format(kind_label),
        "资源摘要：{}".format(summary_line),
    ]
    if phase == "decision":
        lines.append("结果：{}".format({"approved": "通过", "approved_adjusted": "调整后通过", "rejected": "拒绝"}.get(outcome or "", "已处理")))
    lines.extend(["", footer, "审批页面：{}".format(link), ""])
    return subject, "\n".join(lines)[:MAX_BODY_LENGTH]


def application_email_events(config: Dict[str, Any], application_id: str, owner_uid: int,
                             owner_name: str, kind: str, requested: Any,
                             approved: Any = None, phase: str = "created",
                             outcome: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build safe outbox records; no network or credential access occurs here."""
    settings = _config(config)
    health = email_configuration_status(config)
    if not health["enabled"] or not health["configured"]:
        return [], health
    link = application_page_link(settings.get("approval_page_url", ""), application_id)
    source = approved if phase == "decision" and outcome != "rejected" else requested
    summary = safe_application_summary(kind, source)
    admin_recipients = normalize_recipients(settings.get("admin_recipients", []))
    user_recipients = _user_recipients(settings, owner_uid, owner_name)
    events: List[Dict[str, Any]] = []
    if phase == "created":
        subject, body = _render_application_email("admin", "created", application_id, owner_name, kind, summary, link)
        events.append({
            "event_key": "application:{}:created:admin".format(application_id),
            "event_type": "application_created_admin",
            "application_id": application_id,
            "recipient_kind": "admin",
            "recipients": admin_recipients,
            "subject": subject,
            "body": body,
        })
        subject, body = _render_application_email("user", "created", application_id, owner_name, kind, summary, link)
        events.append({
            "event_key": "application:{}:created:user".format(application_id),
            "event_type": "application_created_user",
            "application_id": application_id,
            "recipient_kind": "user",
            "recipients": user_recipients,
            "subject": subject,
            "body": body,
        })
    else:
        subject, body = _render_application_email("user", "decision", application_id, owner_name, kind, summary, link, outcome)
        events.append({
            "event_key": "application:{}:decision:{}:user".format(application_id, outcome or "processed"),
            "event_type": "application_decision_user",
            "application_id": application_id,
            "recipient_kind": "user",
            "recipients": user_recipients,
            "subject": subject,
            "body": body,
        })
    return events, health


def insert_outbox(db: sqlite3.Connection, event: Dict[str, Any]) -> None:
    """Insert idempotently; callers invoke this inside their application tx."""
    recipients = normalize_recipients(event.get("recipients", []))
    suppressed = not recipients
    status = "suppressed" if suppressed else "pending"
    error_code = "no_recipient_configured" if suppressed else None
    now = utc_now()
    message_hash = hashlib.sha256(str(event["event_key"]).encode("utf-8")).hexdigest()[:32]
    db.execute(
        """INSERT OR IGNORE INTO email_outbox(
             id,event_key,event_type,application_id,recipient_kind,recipients_json,
             subject,body,status,attempts,next_attempt_at,locked_until,
             last_error_code,last_error,created_at,updated_at,sent_at,message_id
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            hashlib.sha256((event["event_key"] + ":row").encode("utf-8")).hexdigest(),
            event["event_key"], event["event_type"], event["application_id"],
            event["recipient_kind"], json.dumps(recipients, ensure_ascii=False),
            _clean_text(event.get("subject", "u20 TIME 申请通知"), 240),
            str(event.get("body", ""))[:MAX_BODY_LENGTH], status, 0, now, None,
            error_code, "未配置收件人" if suppressed else None, now, now, None,
            "<u20-{}@u20.invalid>".format(message_hash),
        ),
    )


class DeliveryError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.safe_message = _clean_text(message, MAX_ERROR_LENGTH)


class EmailOutboxWorker:
    """One bounded, restartable worker using its own SQLite connection."""

    def __init__(self, state_db: str, config: Dict[str, Any]):
        self.state_db = state_db
        self.config = config
        self.stop_event = threading.Event()
        self.db: Optional[sqlite3.Connection] = None

    def _connection(self) -> sqlite3.Connection:
        if self.db is None:
            self.db = sqlite3.connect(self.state_db, timeout=10, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA busy_timeout=10000")
            self.db.execute("PRAGMA foreign_keys=ON")
        return self.db

    def _settings(self) -> Dict[str, Any]:
        try:
            row = self._connection().execute("SELECT value FROM settings WHERE key=?", ("email_delivery_state",)).fetchone()
            if row:
                value = json.loads(row[0])
                return value if isinstance(value, dict) else {}
        except (OSError, sqlite3.Error, TypeError, ValueError):
            pass
        return {}

    def _set_state(self, **updates: Any) -> None:
        state = self._settings()
        state.update(updates)
        state["updated_at"] = utc_now()
        try:
            self._connection().execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                ("email_delivery_state", json.dumps(state, ensure_ascii=False), utc_now()),
            )
        except sqlite3.Error:
            # Delivery state is diagnostic only; never turn it into an API or
            # application transaction failure.
            pass

    def _backoff(self, attempt: int) -> int:
        settings = _config(self.config)
        values = settings.get("backoff_seconds", list(DEFAULT_BACKOFF_SECONDS))
        if not isinstance(values, list) or not values:
            values = list(DEFAULT_BACKOFF_SECONDS)
        try:
            value = int(values[min(max(0, attempt - 1), len(values) - 1)])
        except (TypeError, ValueError):
            value = DEFAULT_BACKOFF_SECONDS[min(max(0, attempt - 1), len(DEFAULT_BACKOFF_SECONDS) - 1)]
        return max(1, min(value, 7 * 86400))

    def _max_attempts(self) -> int:
        try:
            return max(1, min(12, int(_config(self.config).get("max_attempts", DEFAULT_MAX_ATTEMPTS))))
        except (TypeError, ValueError):
            return DEFAULT_MAX_ATTEMPTS

    def _claim(self) -> Optional[sqlite3.Row]:
        db = self._connection()
        now = utc_now()
        lock_until = utc_after(max(DEFAULT_SEND_TIMEOUT_SECONDS, int(_config(self.config).get("send_timeout_seconds", DEFAULT_SEND_TIMEOUT_SECONDS))) + LOCK_GRACE_SECONDS)
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                """SELECT * FROM email_outbox
                   WHERE (status='pending' AND next_attempt_at<=?)
                      OR (status='sending' AND locked_until IS NOT NULL AND locked_until<?)
                   ORDER BY next_attempt_at,created_at LIMIT 1""",
                (now, now),
            ).fetchone()
            if not row:
                db.execute("COMMIT")
                return None
            attempts = int(row["attempts"] or 0) + 1
            if attempts > self._max_attempts():
                db.execute(
                    "UPDATE email_outbox SET status='failed',locked_until=NULL,last_error_code=?,last_error=?,updated_at=? WHERE id=?",
                    ("max_attempts_exceeded", "已达到最大重试次数", now, row["id"]),
                )
                db.execute("COMMIT")
                return None
            db.execute(
                "UPDATE email_outbox SET status='sending',attempts=?,locked_until=?,updated_at=? WHERE id=?",
                (attempts, lock_until, now, row["id"]),
            )
            db.execute("COMMIT")
            # Return the immutable message fields plus the incremented attempt
            # count.  The row is not shared with the API connection.
            values = dict(row)
            values["attempts"] = attempts
            return values  # type: ignore[return-value]
        except Exception:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def _read_credentials(self, path_value: Any) -> Optional[Tuple[str, str]]:
        if not path_value:
            return None
        path = pathlib.Path(str(path_value))
        try:
            fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                st = os.fstat(handle.fileno())
                if st.st_uid != 0 or not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) & 0o077:
                    raise DeliveryError("credentials_file_permissions", "凭据文件权限不符合要求")
                if st.st_size > 16384:
                    raise DeliveryError("credentials_file_too_large", "凭据文件过大")
                value = json.load(handle)
        except DeliveryError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError):
            raise DeliveryError("credentials_file_invalid", "凭据文件不可读或格式无效")
        if not isinstance(value, dict) or not isinstance(value.get("username", ""), str) or not isinstance(value.get("password", ""), str):
            raise DeliveryError("credentials_file_invalid", "凭据文件字段无效")
        # Values are returned only to smtplib and are never logged, audited or
        # exposed through the diagnostic API.
        return value.get("username", ""), value.get("password", "")

    def _send(self, row: Dict[str, Any]) -> None:
        settings = _config(self.config)
        smtp_config = settings.get("smtp", {})
        recipients = normalize_recipients(json.loads(row["recipients_json"]))
        if not recipients:
            raise DeliveryError("no_recipient_configured", "未配置收件人")
        try:
            host = str(smtp_config["host"])
            port = int(smtp_config["port"])
            timeout = int(smtp_config.get("send_timeout_seconds", settings.get("send_timeout_seconds", DEFAULT_SEND_TIMEOUT_SECONDS)))
            sender = _sender(smtp_config["sender"])
            mode = smtp_config.get("tls_mode", "starttls")
        except (KeyError, TypeError, ValueError):
            raise DeliveryError("smtp_config_invalid", "SMTP 配置无效")
        if not sender or mode not in ("none", "starttls", "tls"):
            raise DeliveryError("smtp_config_invalid", "SMTP 配置无效")
        credentials = self._read_credentials(smtp_config.get("credentials_file", ""))
        if credentials and mode == "none":
            raise DeliveryError("smtp_tls_required", "SMTP 登录必须使用加密连接")
        message = EmailMessage()
        message["From"] = sender
        message["To"] = ", ".join(recipients)
        message["Subject"] = _clean_text(row["subject"], 240)
        message["Date"] = formatdate(localtime=False)
        message["Message-ID"] = row["message_id"]
        message["Auto-Submitted"] = "auto-generated"
        message["X-Auto-Response-Suppress"] = "All"
        message.set_content(str(row["body"])[:MAX_BODY_LENGTH])
        client: Any = None
        try:
            context = ssl.create_default_context()
            if mode == "tls":
                client = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
            else:
                client = smtplib.SMTP(host, port, timeout=timeout)
                if mode == "starttls":
                    client.starttls(context=context)
            if credentials:
                client.login(credentials[0], credentials[1])
            client.send_message(message)
        except smtplib.SMTPAuthenticationError:
            raise DeliveryError("smtp_authentication_failed", "SMTP 身份验证失败")
        except smtplib.SMTPRecipientsRefused:
            raise DeliveryError("smtp_recipient_rejected", "SMTP 拒绝了收件人")
        except smtplib.SMTPConnectError:
            raise DeliveryError("smtp_connection_failed", "无法连接 SMTP 服务")
        except smtplib.SMTPServerDisconnected:
            raise DeliveryError("smtp_disconnected", "SMTP 连接中断")
        except (smtplib.SMTPException, OSError, socket.timeout, ssl.SSLError):
            raise DeliveryError("smtp_delivery_failed", "SMTP 投递失败")
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    try:
                        client.close()
                    except Exception:
                        pass

    def _mark_sent(self, row: Dict[str, Any]) -> None:
        now = utc_now()
        self._connection().execute(
            "UPDATE email_outbox SET status='sent',locked_until=NULL,last_error_code=NULL,last_error=NULL,sent_at=?,updated_at=? WHERE id=? AND status='sending'",
            (now, now, row["id"]),
        )

    def _mark_failed(self, row: Dict[str, Any], error: DeliveryError) -> None:
        now = utc_now()
        attempts = int(row.get("attempts", 0))
        terminal = attempts >= self._max_attempts()
        status = "failed" if terminal else "pending"
        retry_at = None if terminal else utc_after(self._backoff(attempts))
        self._connection().execute(
            "UPDATE email_outbox SET status=?,next_attempt_at=COALESCE(?,next_attempt_at),locked_until=NULL,last_error_code=?,last_error=?,updated_at=? WHERE id=? AND status='sending'",
            (status, retry_at, error.code, error.safe_message, now, row["id"]),
        )
        self._set_state(last_error_code=error.code, last_error=error.safe_message, last_error_at=now)

    def run_once(self) -> bool:
        health = email_configuration_status(self.config)
        if not health["enabled"]:
            self._set_state(state="disabled", last_error_code=None, last_error=None)
            return False
        if not health["configured"]:
            self._set_state(state="configuration_error", last_error_code=health["error_code"], last_error="邮件配置未就绪")
            return False
        row = self._claim()
        if not row:
            self._set_state(state="idle", last_error_code=None, last_error=None)
            return False
        now = utc_now()
        self._set_state(state="sending", last_attempt_at=now, last_error_code=None, last_error=None)
        try:
            self._send(row)
        except DeliveryError as exc:
            self._mark_failed(row, exc)
        except Exception:
            # Do not serialize arbitrary exception text: it may contain server
            # responses or configuration values.
            self._mark_failed(row, DeliveryError("delivery_error", "邮件投递失败"))
        else:
            self._mark_sent(row)
            self._set_state(state="idle", last_success_at=utc_now(), last_error_code=None, last_error=None)
        return True

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                self._set_state(state="worker_error", last_error_code="worker_error", last_error="邮件投递器暂时不可用")
            settings = _config(self.config)
            try:
                delay = max(1, min(60, int(settings.get("poll_seconds", DEFAULT_POLL_SECONDS))))
            except (TypeError, ValueError):
                delay = DEFAULT_POLL_SECONDS
            self.stop_event.wait(delay)


def start_email_worker(state_db: str, config: Dict[str, Any]) -> Tuple[EmailOutboxWorker, threading.Thread]:
    worker = EmailOutboxWorker(state_db, config)
    thread = threading.Thread(target=worker.run_forever, name="u20-email-outbox", daemon=True)
    thread.start()
    return worker, thread


def outbox_status(db: sqlite3.Connection, config: Dict[str, Any], worker_alive: bool) -> Dict[str, Any]:
    health = email_configuration_status(config)
    counts = {row["status"]: int(row["count"]) for row in db.execute("SELECT status,COUNT(*) AS count FROM email_outbox GROUP BY status")}
    rows = db.execute(
        """SELECT event_type,application_id,status,attempts,next_attempt_at,
                  last_error_code,last_error,created_at,updated_at,sent_at
           FROM email_outbox ORDER BY created_at DESC LIMIT 50"""
    ).fetchall()
    recent = [dict(row) for row in rows]
    state: Dict[str, Any] = {}
    try:
        row = db.execute("SELECT value FROM settings WHERE key=?", ("email_delivery_state",)).fetchone()
        if row:
            value = json.loads(row[0])
            if isinstance(value, dict):
                state = value
    except (sqlite3.Error, TypeError, ValueError):
        state = {}
    return {
        "configuration": {
            "enabled": health["enabled"],
            "configured": health["configured"],
            "error_code": health["error_code"],
            "warnings": health["warnings"],
            "worker_alive": bool(worker_alive),
        },
        "counts": counts,
        "recent": recent,
        "worker": {
            "state": state.get("state"),
            "last_error_code": state.get("last_error_code"),
            "last_error": state.get("last_error"),
            "last_attempt_at": state.get("last_attempt_at"),
            "last_success_at": state.get("last_success_at"),
            "updated_at": state.get("updated_at"),
        },
    }
