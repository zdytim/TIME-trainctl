#!/usr/bin/env python3
"""The user-facing time platform CLI.  Install as `timectl`, never `time`."""

from __future__ import print_function

import argparse
import json
import math
import os
import re
import sys
import time as _time
import unicodedata

from time_core import (BrokerClient, RequestLedger, TimeError, UnknownOutcome,
                       discover_server_capabilities,
                       error_envelope, feature_capabilities, format_error,
                       job_outcome, mutation_result_outcome, normalize_command_job, parse_month, response_envelope,
                       utc_now)


EXIT_OK = 0
EXIT_ARGS = 2
EXIT_FORBIDDEN = 3
EXIT_CONFLICT = 4
EXIT_UNAVAILABLE = 5
EXIT_UNKNOWN = 6


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise TimeError("invalid_arguments", message, status=400)


def cell(value):
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in str(value))


def display_width(value):
    return sum(0 if unicodedata.combining(ch) else
               2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in value)


def table(headers, rows):
    rows = [tuple(map(cell, row)) for row in rows]
    widths = [max(map(display_width, column)) for column in zip(headers, *rows)]
    edge = "+" + "+".join("-" * (width + 2) for width in widths) + "+"

    def line(row):
        return "| " + " | ".join(value + " " * (width - display_width(value))
                                 for value, width in zip(row, widths)) + " |"

    return "\n".join([edge, line(headers), edge, *(line(row) for row in rows), edge])


def gpu_view(state):
    """Limit every returned GPU view to the caller's configured card count."""
    cached = state.get("gpus") or {}
    quota = (state.get("quota") or {}).get("gpu_limit")
    limit = quota if type(quota) is int and quota >= 0 else 0
    processes = cached.get("processes") or []
    uid = (state.get("identity") or {}).get("uid")
    own = {p.get("gpu_uuid") for p in processes if uid is not None and p.get("uid") == uid}
    busy = {p.get("gpu_uuid") for p in processes}
    devices = cached.get("devices") or []
    selected = sorted(devices, key=lambda d: (d.get("id") not in own, d.get("id") in busy))[:limit]
    visible_ids = {d["id"] for d in selected if d.get("id") is not None}
    view = {k: cached[k] for k in (
        "available", "stale", "status", "driver_version", "observed_at", "last_attempt_at",
        "process_visibility", "history_started_at", "service_started_at", "history_scope",
        "sampling_interval_seconds", "age_seconds") if k in cached}
    if cached.get("available") is not True:
        view["reason"] = "GPU telemetry unavailable"
    view.update(
        gpu_limit=quota if type(quota) is int and quota >= 0 else None,
        devices=selected,
        processes=[p for p in processes if p.get("gpu_uuid") in visible_ids],
        history=[{**sample, "devices": [d for d in (sample.get("devices") or [])
                                        if d.get("id") in visible_ids]}
                 for sample in (cached.get("history") or [])],
    )
    metrics = {key: value for key, value in (state.get("metrics") or {}).items() if key != "gpu"}
    return {"gpus": view, "metrics": {**metrics, "gpu": view}}


def number(value, fmt=".0f", unit=""):
    if type(value) not in (int, float) or not math.isfinite(value):
        return "N/A"
    return format(value, fmt) + unit


def gpu_text(gpu):
    driver = gpu.get("driver_version") or "N/A"
    lines = [f"TIME GPU Monitor   Driver Version: {cell(driver)}"]
    if gpu.get("gpu_limit") is None:
        lines.append("Account GPU limit unavailable.")
    if gpu.get("available") is not True:
        lines.append("GPU telemetry unavailable" +
                     (" · showing the last sample." if gpu.get("stale") else "."))
    if gpu.get("observed_at"):
        lines.append(f"Sample: {cell(gpu['observed_at'])}")
    devices = gpu.get("devices") or []
    if not devices:
        return "\n".join([*lines, "No GPUs to display."])
    lines.append(table(
        ("GPU", "Name", "Fan", "Temp", "Perf", "Pwr:Usage/Cap", "Memory-Usage", "GPU-Util"),
        [(d.get("index"), d.get("name"), number(d.get("fan_percent"), unit="%"),
          number(d.get("temp_c"), unit="C"), d.get("pstate") or "N/A",
          f"{number(d.get('power_w'), unit='W')} / {number(d.get('power_limit_w'), unit='W')}",
          f"{number(d.get('memory_used_mb'), unit='MiB')} / {number(d.get('memory_total_mb'), unit='MiB')}",
          number(d.get("utilization_percent"), unit="%")) for d in devices]))
    processes = gpu.get("processes") or []
    lines.append("Processes (your account)" if gpu.get("process_visibility") == "own_only" else "Processes")
    lines.append(table(("GPU", "PID", "Type", "Process", "GPU Memory"),
                       [(p.get("gpu_index"), p.get("pid"), p.get("type"),
                         p.get("process_name") or p.get("command") or p.get("name"),
                         number(p.get("memory_used_mb"), unit="MiB")) for p in processes])
                 if processes else "No visible GPU processes.")
    return "\n".join(lines)


def fields(data, indent=""):
    lines = []
    if isinstance(data, dict):
        for key, value in data.items():
            label = cell(key).replace("_", " ").capitalize()
            if isinstance(value, (dict, list)) and value:
                lines.extend([f"{indent}{label}", *fields(value, indent + "  ")])
            else:
                lines.append(f"{indent}{label}: {cell(value) if value not in ({}, []) else '—'}")
    elif isinstance(data, list):
        for value in data:
            lines.extend(fields(value, indent) if isinstance(value, (dict, list)) else [indent + cell(value)])
    else:
        lines.append(indent + cell(data))
    return lines


def quota_text(data):
    quota, usage, monthly = (data.get(key) or {} for key in ("quota", "usage", "monthly_quota"))
    lines = [
        f"GPU in use: {cell(usage.get('gpu'))} / {cell(quota.get('gpu_limit'))}",
        f"Concurrent jobs: {cell(usage.get('concurrent_jobs'))} / "
        f"{cell(quota.get('concurrent_job_limit', quota.get('concurrent_gpu_limit')))}",
        f"Active jobs: {cell(usage.get('active_jobs'))} / {cell(quota.get('active_job_limit'))}",
        f"Monthly GPU hours · {cell(monthly.get('month'))} · "
        + ("enforced" if monthly.get("enforcement_ready") is True else "status unconfirmed"),
    ]
    verified = monthly.get("usage_verified") is True

    def hours(seconds):
        return number(seconds / 3600, ".3f") if verified and type(seconds) in (int, float) else "—"

    tiers = monthly.get("tiers") or []
    if tiers:
        lines.append(table(("Tier", "Limit", "Used", "Remaining"),
                           [(tier.get("name"), *(hours(tier.get(key)) for key in
                             ("limit_gpu_seconds", "used_gpu_seconds", "remaining_gpu_seconds"))) for tier in tiers]))
    if verified:
        lines.append("Monthly overage: " + hours(monthly.get("overage_gpu_seconds")) + " GPU·h")
    return "\n".join(lines)


def job_time(job):
    minutes = job.get("requested_minutes", job.get("time_minutes"))
    return "Unlimited" if job.get("policy_version") == 2 or minutes == 4294967295 else (
        f"{cell(minutes)} min" if minutes is not None else "—")


def command_text(data, command):
    if not isinstance(data, dict):
        return "\n".join(fields(data))
    if command == "gpu":
        return gpu_text(data["gpus"])
    if command == "quota":
        return quota_text(data)
    if command == "logs":
        if not data.get("available"):
            return cell(data.get("reason") or "Log not available yet.")
        return "".join(ch for ch in (data.get("content") or "")
                       if ch in "\n\t" or not unicodedata.category(ch).startswith("C")).rstrip("\n")
    if isinstance(data.get("jobs"), list):
        return table(("Job ID", "Slurm ID", "State", "Name", "GPU", "Monthly tier", "Time limit"),
                     [(j.get("id"), j.get("scheduler_id"), j.get("state"), j.get("name"),
                       j.get("requested_gpu"), j.get("selected_tier"), job_time(j))
                      for j in data["jobs"]]) if data["jobs"] else "No visible jobs."
    if isinstance(data.get("job"), dict):
        job = data["job"]
        return "\n".join(fields({
            "job ID": job.get("id"), "Slurm ID": job.get("scheduler_id"),
            "name": job.get("name"), "state": job.get("state"),
            "GPU": job.get("requested_gpu", job.get("gpu")), "monthly tier": job.get("selected_tier"),
            "time limit": job_time(job), "reason": job.get("reason_text"),
            "command": job.get("command"), "log": job.get("output_path"),
        }))
    if isinstance(data.get("applications"), list):
        return table(("Application", "Kind", "Status", "Created"),
                     [(a.get("id"), a.get("kind"), a.get("status"), a.get("created_at"))
                      for a in data["applications"]]) if data["applications"] else "No applications."
    return "\n".join(fields(data)) or "OK"


def emit(value, args=None):
    json_requested = bool(args and getattr(args, "json", False))
    pretty = bool(args and getattr(args, "pretty", False))
    text_requested = bool(args and getattr(args, "text", False))
    command = getattr(args, "command", None)
    if json_requested or (command != "gpu" and not text_requested and not sys.stdout.isatty()):
        sys.stdout.write(json.dumps(value, ensure_ascii=False,
                                    sort_keys=True, indent=2 if pretty else None)
                         + "\n")
    else:
        meta = value.get("meta", {}) if isinstance(value, dict) else {}
        if value.get("ok"):
            output = command_text(value.get("data"), command)
            if output:
                sys.stdout.write(output + "\n")
            if meta.get("request_id"):
                sys.stdout.write(f"Request: {cell(meta['request_id'])} · {cell(meta.get('outcome'))}\n")
        else:
            error = value.get("error") or {}
            sys.stdout.write("Error [%s]: %s\n" % (
                cell(error.get("code", "error")), cell(error.get("message", "operation failed"))))
            if meta.get("request_id"):
                sys.stdout.write("request_id: %s\n" % meta["request_id"])
            if error.get("details") is not None:
                sys.stdout.write("\n".join(fields(error["details"], "  ")) + "\n")
        for warning in value.get("warnings") or []:
            sys.stdout.write("Warning: " + cell(warning) + "\n")
    sys.stdout.flush()


def parser_error(message):
    raise TimeError("invalid_arguments", message, status=400)


def add_job_options(parser):
    parser.add_argument("--name", default=None)
    parser.add_argument("--gpus", default=1)
    parser.add_argument("--cpus", default=1)
    parser.add_argument("--mem", default="1GiB")
    parser.add_argument("--tier", choices=("debug", "important", "normal"), default=None,
                        help="required monthly GPU-hour tier")
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--environment", default="system")
    parser.add_argument("--client-request-id", default=None)
    parser.add_argument("--temporary-application-id", default=None)


def build_parser():
    parser = JsonArgumentParser(
        prog="timectl",
        description="time platform client; uses the existing root-broker Unix socket")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    output.add_argument("--text", action="store_true", help="show tables even when redirected or piped")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--state-dir", default=None, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command")

    commands.add_parser("whoami", help="show broker-authenticated identity")
    commands.add_parser("doctor", help="check identity and broker state")
    commands.add_parser("capabilities", help="show honest client/server capability boundary")
    queue = commands.add_parser("queue", help="list visible jobs")
    queue.add_argument("--state", default=None)
    show = commands.add_parser("show", help="show one exact or unambiguous job ID")
    show.add_argument("job_id")
    gpu = commands.add_parser("gpu", help="show broker-reported GPU state")
    gpu.add_argument("--watch", action="store_true", help="not available without a polling contract")
    commands.add_parser("quota", help="show concurrent quota and monthly status")
    logs = commands.add_parser("logs", help="show bounded log tail")
    logs.add_argument("job_id")
    logs.add_argument("--follow", action="store_true")

    run = commands.add_parser("run", help="validate or submit a shell-compatible batch command")
    add_job_options(run)
    run.add_argument("--dry-run", action="store_true", help="validate only; never queues a job")
    run.add_argument("--yes", action="store_true", help="explicitly authorize mutation")
    run.add_argument("command_argv", nargs=argparse.REMAINDER)

    validate = commands.add_parser("validate", help="validate a batch command without queuing")
    add_job_options(validate)
    validate.add_argument("command_argv", nargs=argparse.REMAINDER)

    cancel = commands.add_parser("cancel", help="request cancellation of one job")
    cancel.add_argument("job_id")
    cancel.add_argument("--yes", action="store_true")

    requests = commands.add_parser("request", help="list/create/show resource applications")
    request_sub = requests.add_subparsers(dest="request_command")
    request_sub.add_parser("list")
    request_show = request_sub.add_parser("show")
    request_show.add_argument("application_id")
    request_create = request_sub.add_parser("create")
    request_create.add_argument("--kind", required=True,
                                choices=("temporary_job", "quota_change", "monthly_gpu_hours"))
    request_create.add_argument("--reason", required=True)
    request_create.add_argument("--gpus", type=int, default=1)
    request_create.add_argument("--cpus", type=int, default=1)
    request_create.add_argument("--mem", default="1GiB")
    request_create.add_argument("--gpu-hours", type=int)
    request_create.add_argument("--month", default=None)
    request_create.add_argument("--client-request-id", default=None)
    request_create.add_argument("--gpu-limit", type=int)
    request_create.add_argument("--concurrent-gpu-limit", type=int)
    request_create.add_argument("--active-job-limit", type=int)
    request_create.add_argument("--name", default=None)
    request_create.add_argument("--workdir", default=None)
    request_create.add_argument("--output", default=None)
    request_create.add_argument("--environment", default="system")
    request_create.add_argument("command_argv", nargs=argparse.REMAINDER)
    request_create.add_argument("--yes", action="store_true")

    admin = commands.add_parser("admin", help="existing administrator-only broker operations")
    admin_sub = admin.add_subparsers(dest="admin_command")
    admin_req = admin_sub.add_parser("request")
    admin_req_sub = admin_req.add_subparsers(dest="request_action")
    approve = admin_req_sub.add_parser("approve")
    approve.add_argument("application_id")
    approve.add_argument("--yes", action="store_true")
    approve.add_argument("--reason", default="")
    approve.add_argument("--adjusted", action="store_true")
    approve.add_argument("--approved-json", default=None)
    reject = admin_req_sub.add_parser("reject")
    reject.add_argument("application_id")
    reject.add_argument("--reason", required=True)
    reject.add_argument("--yes", action="store_true")
    reconcile = admin_req_sub.add_parser("reconcile", help="reconcile one pending monthly grant")
    reconcile.add_argument("application_id")
    reconcile.add_argument("--yes", action="store_true")
    admin_preview = admin_sub.add_parser("quota-preview")
    admin_preview.add_argument("--scope", choices=("default", "user"), default="user")
    admin_preview.add_argument("--uid", type=int)
    admin_preview.add_argument("--inherit", action="store_true")
    admin_preview.add_argument("--gpu-limit", type=int)
    admin_preview.add_argument("--concurrent-gpu-limit", type=int)
    admin_preview.add_argument("--active-job-limit", type=int)
    admin_preview.add_argument("--yes", action="store_true")
    admin_apply = admin_sub.add_parser("quota-apply")
    admin_apply.add_argument("--preview-id", required=True)
    admin_apply.add_argument("--yes", action="store_true")
    rules_preview = admin_sub.add_parser("rules-preview")
    rules_preview.add_argument("--rules-json", required=True)
    rules_preview.add_argument("--yes", action="store_true")
    rules_apply = admin_sub.add_parser("rules-apply")
    rules_apply.add_argument("--preview-id", required=True)
    rules_apply.add_argument("--yes", action="store_true")
    admin_sub.add_parser("audit")
    local = commands.add_parser("request-status", help="recover a durable local mutation record")
    local.add_argument("request_id")
    return parser


def strip_command_terminator(argv):
    values = list(argv or [])
    if values and values[0] == "--":
        values.pop(0)
    return values


def make_job(args):
    return normalize_command_job(
        strip_command_terminator(args.command_argv), name=args.name,
        gpus=args.gpus, cpus=args.cpus, memory=args.mem,
        workdir=args.workdir,
        output=args.output, environment=args.environment, selected_tier=args.tier)


def mutation_allowed(args):
    if getattr(args, "yes", False):
        return
    if not sys.stdin.isatty():
        raise TimeError("confirmation_required",
                        "non-interactive mutations require --yes",
                        status=400)
    question = getattr(args, "confirmation_text", None) or "Authorize this mutation? [y/N] "
    sys.stderr.write(question)
    sys.stderr.flush()
    answer = sys.stdin.readline().strip().lower()
    if answer not in ("y", "yes"):
        raise TimeError("confirmation_declined", "mutation was not authorized", status=400)


def discover_capabilities(client):
    return discover_server_capabilities(client)


def client_and_ledger(args):
    # Read-only operations must remain available when local durable mutation
    # state is unavailable. Every operation that may write still needs it.
    needs_ledger = (
        args.command in ("request-status", "cancel") or
        (args.command == "run" and not args.dry_run) or
        (args.command == "request" and args.request_command == "create") or
        (args.command == "admin" and args.admin_command not in (None, "audit"))
    )
    return BrokerClient(), RequestLedger(args.state_dir) if needs_ledger else None


def request_read(client, method, path, body=None, timeout=10):
    return client.request(method, path, body, timeout=timeout, mutation=False)


def request_write(client, ledger, request_id, method, path, body, timeout):
    try:
        ledger.reserve(request_id, method, path, body)
    except TimeError as exc:
        exc.request_id = request_id
        raise
    try:
        data = client.request(method, path, body, timeout=timeout, mutation=True)
    except TimeError as exc:
        exc.request_id = request_id
        try:
            ledger.update(request_id, outcome=exc.outcome, error=error_envelope(exc))
        except (TimeError, OSError):
            # Preserve the actual server/transport outcome and request ID.
            pass
        raise
    outcome = mutation_result_outcome(method, path, data)
    try:
        ledger.update(request_id, outcome=outcome, result=data)
    except (TimeError, OSError) as exc:
        error = UnknownOutcome("server replied but local request record could not be updated; inspect before retrying", details=str(exc))
        error.request_id = request_id
        raise error
    return data, outcome


def command_jobs(client):
    return request_read(client, "GET", "/api/v1/jobs").get("jobs", [])


def find_job(jobs, value):
    exact = [job for job in jobs if str(job.get("id")) == value]
    if len(exact) == 1:
        return exact[0]
    prefix = [job for job in jobs if str(job.get("id", "")).startswith(value)]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        raise TimeError("ambiguous_job_id", "job ID prefix matches multiple jobs",
                        status=409, details=[job.get("id") for job in prefix])
    raise TimeError("job_not_found", "job ID was not found", status=404)


def state_value(client):
    return request_read(client, "GET", "/api/v1/state")


def authenticated_uid(client):
    identity = request_read(client, "GET", "/api/v1/me").get("identity")
    uid = identity.get("uid") if isinstance(identity, dict) else None
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
        raise TimeError("identity_unavailable", "cannot establish the current request owner", status=503)
    return uid


def request_status(client, ledger, request_id):
    record = ledger.get(request_id)
    if not record:
        raise TimeError("request_not_found", "request ID is not in local ledger", status=404)
    # Job request IDs only identify POST /jobs.  Do not correlate a
    # cancellation, approval or other mutation with an unrelated job.
    if (record.get("outcome") in ("pending", "unknown") and
            record.get("method") == "POST" and record.get("path") == "/api/v1/jobs"):
        try:
            uid = authenticated_uid(client)
            jobs = command_jobs(client)
            matches = [job for job in jobs
                       if type(job.get("owner_uid")) is int and job.get("owner_uid") == uid and
                       job.get("client_request_id") == request_id]
            if len(matches) == 1:
                record = dict(record)
                record["outcome"] = job_outcome(matches[0], mutation=True)
                record["result"] = {"job": matches[0]}
                ledger.update(request_id, outcome=record["outcome"], result=record["result"])
        except TimeError:
            pass
    # A cancellation is correlated by its explicit target job, never by
    # the submit request ID. A terminal non-cancelled state is not proof
    # that the cancellation succeeded.
    cancel_path = re.fullmatch(r"/api/v1/jobs/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/cancel",
                               record.get("path", ""))
    if (record.get("outcome") in ("pending", "unknown", "accepted_pending") and
            record.get("method") == "POST" and cancel_path):
        try:
            jobs = command_jobs(client)
            matches = [job for job in jobs if str(job.get("id")) == cancel_path.group(1)]
            if len(matches) == 1:
                job = matches[0]
                state = str(job.get("state", "")).upper()
                reason = str(job.get("reason_code", "")).lower()
                if state in ("CANCELLED", "CANCELED"):
                    outcome = "confirmed"
                elif state in ("COMPLETED", "FAILED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY"):
                    outcome = "unknown"
                elif reason == "cancellation_requested" or state == "CANCELLATION_REQUESTED":
                    outcome = "accepted_pending"
                else:
                    outcome = "unknown"
                record = dict(record)
                record["outcome"] = outcome
                record["result"] = {"job": job}
                ledger.update(request_id, outcome=outcome, result=record["result"])
        except TimeError:
            pass
    if (record.get("outcome") in ("pending", "unknown") and
            record.get("method") == "POST" and record.get("path") == "/api/v1/applications" and
            record.get("body", {}).get("kind") == "monthly_gpu_hours"):
        try:
            body = record["body"]
            uid = authenticated_uid(client)
            applications = request_read(client, "GET", "/api/v1/applications").get("applications", [])
            matches = [item for item in applications if item.get("kind") == "monthly_gpu_hours" and
                       type(item.get("owner_uid")) is int and item.get("owner_uid") == uid and
                       item.get("requested") == body.get("requested") and item.get("reason") == body.get("reason")]
            if len(matches) == 1:
                record = dict(record)
                record["outcome"] = "confirmed"
                record["result"] = {"application": matches[0]}
                ledger.update(request_id, outcome="confirmed", result=record["result"])
        except TimeError:
            pass
    return response_envelope(True, record, request_id=request_id,
                             outcome=record.get("outcome", "unknown"))


def handle(args):
    client, ledger = client_and_ledger(args)
    command = args.command
    if command == "capabilities":
        return response_envelope(True, feature_capabilities(discover_capabilities(client)))
    if command == "request-status":
        return request_status(client, ledger, args.request_id)
    if command == "whoami":
        return response_envelope(True, request_read(client, "GET", "/api/v1/me"))
    if command == "doctor":
        identity = request_read(client, "GET", "/api/v1/me")
        result = {"backend_reachable": True, "identity": identity,
                  "capabilities": feature_capabilities({
                      "state": "reachable", "identity_version": identity.get("version"),
                      "route": "GET /api/v1/me"})}
        try:
            state = state_value(client)
            result["scheduler_available"] = state.get("scheduler_available")
            result["scheduler_error"] = state.get("scheduler_error")
        except TimeError as exc:
            result["state_error"] = {"code": exc.code, "message": exc.message}
        return response_envelope(True, result)
    if command == "queue":
        jobs = command_jobs(client)
        if args.state:
            jobs = [job for job in jobs if str(job.get("state")) == args.state]
        return response_envelope(True, {"jobs": jobs})
    if command == "show":
        return response_envelope(True, {"job": find_job(command_jobs(client), args.job_id)})
    if command == "gpu":
        if args.watch:
            raise TimeError("feature_not_supported", "GPU watch has no streaming contract; use gpu repeatedly", status=409)
        return response_envelope(True, gpu_view(state_value(client)))
    if command == "quota":
        state = state_value(client)
        return response_envelope(True, {
            "quota": state.get("quota"),
            "usage": state.get("usage"),
            "monthly_quota": state.get("monthly_quota", {
                "state": "unknown", "enforcement_ready": "unknown",
                "reason": "legacy state response has no monthly quota field"}),
            "monthly_enforcement": feature_capabilities(discover_capabilities(client))["monthly_quota"],
        })
    if command == "logs":
        if args.follow:
            raise TimeError("feature_not_supported", "backend exposes only a bounded log tail", status=409)
        job = find_job(command_jobs(client), args.job_id)
        data = request_read(client, "GET", "/api/v1/jobs/%s/log" % job["id"], timeout=5)
        data["tail_limit_bytes"] = 1048576
        data["follow"] = False
        return response_envelope(True, data)
    if command in ("run", "validate"):
        temporary_id = getattr(args, "temporary_application_id", None)
        if temporary_id:
            if command == "validate" or args.dry_run or strip_command_terminator(args.command_argv):
                raise TimeError("invalid_arguments", "an approved temporary application must be submitted alone without a command; dry-run is unavailable", status=400)
            job = {"temporary_application_id": temporary_id}
        else:
            job = make_job(args)
        if command == "validate" or args.dry_run:
            data = request_read(client, "POST", "/api/v1/jobs/validate", job,
                                timeout=10)
            return response_envelope(True, data)
        if not temporary_id:
            args.confirmation_text = "Submit without a per-job time limit or automatic requeue? GPU usage is billed to the selected monthly quota. [y/N] "
        mutation_allowed(args)
        request_id = args.client_request_id or ledger.new_id()
        job["client_request_id"] = request_id
        data, outcome = request_write(client, ledger, request_id, "POST",
                                      "/api/v1/jobs", job, timeout=45)
        return response_envelope(True, data, request_id=request_id, outcome=outcome)
    if command == "cancel":
        jobs = command_jobs(client)
        job = find_job(jobs, args.job_id)
        args.confirmation_text = "Cancel task %s (%s)? [y/N] " % (job.get("name", ""), job["id"])
        mutation_allowed(args)
        request_id = ledger.new_id()
        data, outcome = request_write(client, ledger, request_id, "POST",
                                      "/api/v1/jobs/%s/cancel" % job["id"], {},
                                      timeout=15)
        return response_envelope(True, data, request_id=request_id, outcome=outcome)
    if command == "request":
        if args.request_command in (None, "list"):
            return response_envelope(True, {"applications": request_read(client, "GET", "/api/v1/applications").get("applications", [])})
        applications = request_read(client, "GET", "/api/v1/applications").get("applications", [])
        if args.request_command == "show":
            found = [x for x in applications if x.get("id") == args.application_id]
            if not found:
                raise TimeError("application_not_found", "application was not found", status=404)
            return response_envelope(True, {"application": found[0]})
        if args.request_command == "create":
            mutation_allowed(args)
            if args.kind == "monthly_gpu_hours":
                discovery = discover_capabilities(client)
                observed = discovery.get("capabilities", {})
                capability = observed.get("monthly_requests", {}) if isinstance(observed, dict) else {}
                if not isinstance(capability, dict) or capability.get("available") is not True or capability.get("idempotency") != "owner-request-id-v1":
                    raise TimeError("feature_not_enabled", "server has not advertised the idempotent monthly application contract", status=409)
                try:
                    month = parse_month(args.month)
                except (TypeError, ValueError):
                    raise TimeError("invalid_arguments", "--month must be YYYY-MM or YYYYMM", status=400)
                if args.gpu_hours is None or not 1 <= args.gpu_hours <= 100000:
                    raise TimeError("invalid_arguments", "provide --gpu-hours (1..100000) and --month YYYY-MM", status=400)
                request_id = args.client_request_id or ledger.new_id()
                requested = {"gpu_hours": args.gpu_hours, "month": month, "client_request_id": request_id}
            elif args.kind in ("temporary_job", "quota_change"):
                raise TimeError("feature_not_supported", "legacy application creation lacks server idempotency", status=409)
            else:
                raise TimeError("invalid_arguments", "unsupported application kind", status=400)
            body = {"kind": args.kind, "requested": requested, "reason": args.reason}
            data, outcome = request_write(client, ledger, request_id, "POST",
                                          "/api/v1/applications", body, timeout=15)
            return response_envelope(True, data, request_id=request_id, outcome=outcome)
    if command == "admin":
        return handle_admin(args, client, ledger)
    raise TimeError("invalid_arguments", "a command is required", status=400)


def handle_admin(args, client, ledger):
    if args.admin_command == "request":
        if args.request_action not in ("approve", "reject", "reconcile"):
            raise TimeError("invalid_arguments", "admin request requires approve, reject or reconcile", status=400)
        mutation_allowed(args)
        if args.request_action == "reconcile":
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", args.application_id):
                raise TimeError("invalid_arguments", "invalid application ID", status=400)
            discovery = discover_capabilities(client)
            observed = discovery.get("capabilities", {})
            capability = observed.get("monthly_requests", {}) if isinstance(observed, dict) else {}
            if not isinstance(capability, dict) or capability.get("reconcile") != "application-grant-v1":
                raise TimeError("feature_not_enabled", "server has not advertised monthly grant reconciliation", status=409)
            request_id = ledger.new_id()
            data, outcome = request_write(client, ledger, request_id, "POST",
                "/api/v1/applications/%s/grant-reconcile" % args.application_id, {}, timeout=15)
            return response_envelope(True, data, request_id=request_id, outcome=outcome)
        adjusted = bool(getattr(args, "adjusted", False))
        body = {"action": "reject" if args.request_action == "reject" else (
            "approve_adjusted" if adjusted else "approve")}
        if args.reason:
            body["decision_reason"] = args.reason
        if adjusted:
            if not args.approved_json:
                raise TimeError("invalid_arguments",
                                "adjusted approval requires --approved-json with actual approved parameters",
                                status=400)
            try:
                approved = json.loads(args.approved_json)
            except ValueError as exc:
                raise TimeError("invalid_arguments", "--approved-json must be JSON", status=400, details=str(exc))
            if not isinstance(approved, dict) or not approved:
                raise TimeError("invalid_arguments", "--approved-json must be a non-empty object", status=400)
            body["approved"] = approved
        request_id = ledger.new_id()
        data, outcome = request_write(
            client, ledger, request_id, "POST",
            "/api/v1/applications/%s/decision" % args.application_id,
            body, timeout=15)
        return response_envelope(True, data, request_id=request_id, outcome=outcome)
    if args.admin_command == "audit":
        return response_envelope(True, request_read(client, "GET", "/api/v1/audit"))
    if args.admin_command == "quota-preview":
        mutation_allowed(args)
        if args.scope == "user" and args.uid is None:
            raise TimeError("invalid_arguments", "--uid is required for user scope", status=400)
        quota = {}
        for key, value in (("gpu_limit", args.gpu_limit),
                           ("concurrent_gpu_limit", args.concurrent_gpu_limit),
                           ("active_job_limit", args.active_job_limit)):
            if value is not None:
                quota[key] = value
        body = {"scope": args.scope, "uid": args.uid, "inherit": args.inherit,
                "quota": quota}
        request_id = ledger.new_id()
        data, outcome = request_write(client, ledger, request_id, "POST",
                                      "/api/v1/quotas/preview", body, timeout=15)
        return response_envelope(True, data, request_id=request_id, outcome=outcome)
    if args.admin_command == "quota-apply":
        mutation_allowed(args)
        request_id = ledger.new_id()
        data, outcome = request_write(client, ledger, request_id, "POST",
                                      "/api/v1/quotas/apply",
                                      {"preview_id": args.preview_id}, timeout=15)
        return response_envelope(True, data, request_id=request_id, outcome=outcome)
    if args.admin_command == "rules-preview":
        mutation_allowed(args)
        try:
            rules = json.loads(args.rules_json)
        except ValueError as exc:
            raise TimeError("invalid_arguments", "--rules-json must be JSON", status=400, details=str(exc))
        request_id = ledger.new_id()
        data, outcome = request_write(client, ledger, request_id, "POST",
                                      "/api/v1/rules/preview", {"rules": rules}, timeout=15)
        return response_envelope(True, data, request_id=request_id, outcome=outcome)
    if args.admin_command == "rules-apply":
        mutation_allowed(args)
        request_id = ledger.new_id()
        data, outcome = request_write(client, ledger, request_id, "POST",
                                      "/api/v1/rules/apply",
                                      {"preview_id": args.preview_id}, timeout=15)
        return response_envelope(True, data, request_id=request_id, outcome=outcome)
    raise TimeError("invalid_arguments", "unknown admin command", status=400)


def exit_code(exc):
    if isinstance(exc, UnknownOutcome) or exc.unknown:
        return EXIT_UNKNOWN
    if exc.status in (401, 403) or exc.code in ("not_authorized", "admin_required", "job_forbidden"):
        return EXIT_FORBIDDEN
    if exc.status == 503 or exc.code in ("backend_unavailable", "scheduler_unavailable"):
        return EXIT_UNAVAILABLE
    if exc.status in (409, 423) or exc.code in (
            "permanent_quota_exceeded", "active_job_limit", "machine_unsatisfiable",
            "ambiguous_job_id", "preview_stale", "preview_required",
            "feature_not_enabled", "feature_not_supported", "idempotency_conflict"):
        return EXIT_CONFLICT
    if exc.status == 400 or exc.code == "invalid_arguments":
        return EXIT_ARGS
    return EXIT_UNAVAILABLE


def main(argv=None):
    parser = build_parser()
    format_args = argparse.Namespace()
    try:
        raw = list(sys.argv[1:] if argv is None else argv)
        filtered = []
        command_boundary = False
        for value in raw:
            if value == "--":
                command_boundary = True
            if not command_boundary and value in ("--json", "--text", "--pretty"):
                setattr(format_args, value[2:], True)
                continue
            filtered.append(value)
        if getattr(format_args, "json", False) and getattr(format_args, "text", False):
            parser_error("--json and --text cannot be combined")
        args = parser.parse_args(filtered)
        vars(args).update(vars(format_args))
        format_args.command = args.command
        if not args.command:
            parser_error("a command is required")
        result = handle(args)
        emit(result, args)
        outcome = result.get("meta", {}).get("outcome")
        return EXIT_UNKNOWN if outcome == "unknown" else EXIT_CONFLICT if outcome == "rejected" else EXIT_OK
    except TimeError as exc:
        emit(format_error(exc, request_id=getattr(exc, "request_id", None) or getattr(locals().get("args", None), "client_request_id", None)),
             format_args)
        return exit_code(exc)
    except (ValueError, TypeError) as exc:
        error = TimeError("invalid_arguments", str(exc), status=400)
        emit(format_error(error), format_args)
        return EXIT_ARGS
    except KeyboardInterrupt:
        error = TimeError("interrupted", "operation interrupted; no cancellation was sent", status=499)
        emit(format_error(error), format_args)
        return EXIT_UNKNOWN


if __name__ == "__main__":
    raise SystemExit(main())
