#!/usr/bin/env python3
"""The user-facing time platform CLI.  Install as `timectl`, never `time`."""

from __future__ import print_function

import argparse
import json
import os
import sys
import time as _time

from time_core import (BrokerClient, RequestLedger, TimeError, UnknownOutcome,
                       error_envelope, feature_capabilities, format_error,
                       job_outcome, normalize_command_job, response_envelope,
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


def emit(value, pretty=False):
    sys.stdout.write(json.dumps(value, ensure_ascii=False,
                                sort_keys=True, indent=2 if pretty else None)
                     + "\n")
    sys.stdout.flush()


def parser_error(message):
    raise TimeError("invalid_arguments", message, status=400)


def add_job_options(parser):
    parser.add_argument("--name", default=None)
    parser.add_argument("--gpus", default=1)
    parser.add_argument("--cpus", default=1)
    parser.add_argument("--mem", default="1GiB")
    parser.add_argument("--time", dest="duration", default="60m")
    parser.add_argument("--qos", default=None)
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--environment", default="system")
    parser.add_argument("--client-request-id", default=None)
    parser.add_argument("--temporary-application-id", default=None)


def build_parser():
    parser = JsonArgumentParser(
        prog="timectl",
        description="time platform client; uses the existing root-broker Unix socket")
    parser.add_argument("--json", action="store_true", help="kept for explicit JSON intent")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--socket", default=None, help=argparse.SUPPRESS)
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
    request_create.add_argument("--time", dest="duration", default="60m")
    request_create.add_argument("--qos", default="normal")
    request_create.add_argument("--gpu-hours", type=int)
    request_create.add_argument("--month", default=None)
    request_create.add_argument("--client-request-id", default=None)
    request_create.add_argument("--gpu-limit", type=int)
    request_create.add_argument("--concurrent-gpu-limit", type=int)
    request_create.add_argument("--active-job-limit", type=int)
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
    reject = admin_req_sub.add_parser("reject")
    reject.add_argument("application_id")
    reject.add_argument("--reason", required=True)
    reject.add_argument("--yes", action="store_true")
    admin_preview = admin_sub.add_parser("quota-preview")
    admin_preview.add_argument("--scope", choices=("default", "user"), default="user")
    admin_preview.add_argument("--uid", type=int)
    admin_preview.add_argument("--inherit", action="store_true")
    admin_preview.add_argument("--gpu-limit", type=int)
    admin_preview.add_argument("--concurrent-gpu-limit", type=int)
    admin_preview.add_argument("--active-job-limit", type=int)
    admin_apply = admin_sub.add_parser("quota-apply")
    admin_apply.add_argument("--preview-id", required=True)
    admin_apply.add_argument("--yes", action="store_true")
    rules_preview = admin_sub.add_parser("rules-preview")
    rules_preview.add_argument("--rules-json", required=True)
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
        duration=args.duration, qos=args.qos, workdir=args.workdir,
        output=args.output, environment=args.environment)


def mutation_allowed(args):
    if getattr(args, "yes", False):
        return
    if not sys.stdin.isatty():
        raise TimeError("confirmation_required",
                        "non-interactive mutations require --yes",
                        status=400)
    raise TimeError("confirmation_required",
                    "repeat the command with --yes to authorize this mutation",
                    status=400)


def client_and_ledger(args):
    return BrokerClient("/run/pro6000d-training/backend.sock"), None


def request_read(client, method, path, body=None, timeout=10):
    return client.request(method, path, body, timeout=timeout, mutation=False)


def request_write(client, ledger, request_id, method, path, body, timeout):
    ledger.reserve(request_id, method, path, body)
    try:
        data = client.request(method, path, body, timeout=timeout, mutation=True)
    except TimeError as exc:
        ledger.update(request_id, outcome=exc.outcome, error=error_envelope(exc))
        raise
    outcome = "confirmed"
    if isinstance(data, dict) and "job" in data:
        outcome = job_outcome(data.get("job"), mutation=True)
    ledger.update(request_id, outcome=outcome, result=data)
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


def handle(args):
    client, ledger = client_and_ledger(args)
    command = args.command
    # Preview has no mutation authority. Validation is read-only in the broker.
    if (command in ("cancel", "request-status") or
        (command == "run" and not args.dry_run) or
        (command == "request" and args.request_command == "create") or
        (command == "admin" and args.admin_command != "audit")):
        raise TimeError("preview_read_only", "read-only preview: write commands are not available", status=409)
    if command == "capabilities":
        return response_envelope(True, feature_capabilities())
    if command == "request-status":
        record = ledger.get(args.request_id)
        if not record:
            raise TimeError("request_not_found", "request ID is not in local ledger", status=404)
        if record.get("outcome") in ("pending", "unknown"):
            try:
                jobs = command_jobs(client)
                matches = [job for job in jobs
                           if job.get("client_request_id") == args.request_id]
                if len(matches) == 1:
                    record = dict(record)
                    record["outcome"] = job_outcome(matches[0], mutation=True)
                    record["result"] = {"job": matches[0]}
                    ledger.update(args.request_id, outcome=record["outcome"], result=record["result"])
            except TimeError:
                pass
        return response_envelope(True, record, request_id=args.request_id,
                                 outcome=record.get("outcome", "unknown"))
    if command == "whoami":
        return response_envelope(True, request_read(client, "GET", "/api/v1/me"))
    if command == "doctor":
        identity = request_read(client, "GET", "/api/v1/me")
        result = {"backend_reachable": True, "identity": identity,
                  "capabilities": feature_capabilities()}
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
        state = state_value(client)
        return response_envelope(True, {"gpus": state.get("gpus"), "metrics": state.get("metrics")})
    if command == "quota":
        state = state_value(client)
        return response_envelope(True, {
            "quota": state.get("quota"),
            "usage": state.get("usage"),
            "monthly_quota": state.get("monthly_quota", {
                "state": "unknown", "enforcement_ready": False,
                "reason": "legacy state response has no monthly quota field"}),
            "monthly_enforcement": feature_capabilities()["monthly_quota"],
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
        job = make_job(args)
        if command == "validate" or args.dry_run:
            data = request_read(client, "POST", "/api/v1/jobs/validate", job,
                                timeout=10)
            return response_envelope(True, data)
        mutation_allowed(args)
        request_id = args.client_request_id or ledger.new_id()
        job["client_request_id"] = request_id
        if args.temporary_application_id:
            job["temporary_application_id"] = args.temporary_application_id
        data, outcome = request_write(client, ledger, request_id, "POST",
                                      "/api/v1/jobs", job, timeout=45)
        return response_envelope(True, data, request_id=request_id, outcome=outcome)
    if command == "cancel":
        mutation_allowed(args)
        request_id = ledger.new_id()
        jobs = command_jobs(client)
        job = find_job(jobs, args.job_id)
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
                raise TimeError("feature_not_enabled", "monthly quota enforcement is disabled; no monthly request is accepted", status=409)
            if args.kind == "temporary_job":
                requested = normalize_command_job(
                    ["requested-job"], gpus=args.gpus, cpus=args.cpus,
                    memory=args.mem, duration=args.duration, qos=args.qos)
            elif args.kind == "quota_change":
                requested = {}
                for key, value in (("gpu_limit", args.gpu_limit),
                                   ("concurrent_gpu_limit", args.concurrent_gpu_limit),
                                   ("active_job_limit", args.active_job_limit)):
                    if value is not None:
                        requested[key] = value
            else:
                requested = {}
            body = {"kind": args.kind, "requested": requested, "reason": args.reason}
            request_id = ledger.new_id()
            data, outcome = request_write(client, ledger, request_id, "POST",
                                          "/api/v1/applications", body, timeout=15)
            return response_envelope(True, data, request_id=request_id, outcome=outcome)
    if command == "admin":
        return handle_admin(args, client, ledger)
    raise TimeError("invalid_arguments", "a command is required", status=400)


def handle_admin(args, client, ledger):
    if args.admin_command == "request":
        if args.request_action not in ("approve", "reject"):
            raise TimeError("invalid_arguments", "admin request requires approve or reject", status=400)
        mutation_allowed(args)
        body = {"action": "reject" if args.request_action == "reject" else (
            "approve_adjusted" if args.adjusted else "approve")}
        if args.reason:
            body["decision_reason"] = args.reason
        request_id = ledger.new_id()
        data, outcome = request_write(
            client, ledger, request_id, "POST",
            "/api/v1/applications/%s/decision" % args.application_id,
            body, timeout=15)
        return response_envelope(True, data, request_id=request_id, outcome=outcome)
    if args.admin_command == "audit":
        return response_envelope(True, request_read(client, "GET", "/api/v1/audit"))
    if args.admin_command == "quota-preview":
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
        return response_envelope(True, request_read(client, "POST", "/api/v1/quotas/preview", body))
    if args.admin_command == "quota-apply":
        mutation_allowed(args)
        request_id = ledger.new_id()
        data, outcome = request_write(client, ledger, request_id, "POST",
                                      "/api/v1/quotas/apply",
                                      {"preview_id": args.preview_id}, timeout=15)
        return response_envelope(True, data, request_id=request_id, outcome=outcome)
    if args.admin_command == "rules-preview":
        try:
            rules = json.loads(args.rules_json)
        except ValueError as exc:
            raise TimeError("invalid_arguments", "--rules-json must be JSON", status=400, details=str(exc))
        return response_envelope(True, request_read(client, "POST", "/api/v1/rules/preview", {"rules": rules}))
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
    try:
        args = parser.parse_args(argv)
        if not args.command:
            parser_error("a command is required")
        result = handle(args)
        emit(result, pretty=args.pretty)
        return EXIT_UNKNOWN if result.get("meta", {}).get("outcome") == "unknown" else EXIT_OK
    except TimeError as exc:
        emit(format_error(exc, request_id=getattr(locals().get("args", None), "client_request_id", None)),
             pretty=bool(locals().get("args") and locals()["args"].pretty))
        return exit_code(exc)
    except (ValueError, TypeError) as exc:
        error = TimeError("invalid_arguments", str(exc), status=400)
        emit(format_error(error), pretty=bool(locals().get("args") and locals()["args"].pretty))
        return EXIT_ARGS
    except KeyboardInterrupt:
        error = TimeError("interrupted", "operation interrupted; no cancellation was sent", status=499)
        emit(format_error(error))
        return EXIT_UNKNOWN


if __name__ == "__main__":
    raise SystemExit(main())
