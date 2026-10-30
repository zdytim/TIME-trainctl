#!/usr/bin/env python3
"""The user-facing time platform CLI.  Install as `timectl`, never `time`."""

from __future__ import print_function

import argparse
import json
import os
import re
import sys
import time as _time

from time_core import (BrokerClient, RequestLedger, TimeError, UnknownOutcome,
                       discover_server_capabilities,
                       error_envelope, feature_capabilities, format_error,
                       job_outcome, mutation_result_outcome, normalize_command_job, response_envelope,
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


def emit(value, args=None):
    json_requested = bool(args and getattr(args, "json", False))
    pretty = bool(args and getattr(args, "pretty", False))
    if json_requested or not sys.stdout.isatty():
        sys.stdout.write(json.dumps(value, ensure_ascii=False,
                                    sort_keys=True, indent=2 if pretty else None)
                         + "\n")
    else:
        meta = value.get("meta", {}) if isinstance(value, dict) else {}
        outcome = str(meta.get("outcome", "unknown")).upper()
        if value.get("ok"):
            sys.stdout.write("outcome: %s\n" % outcome)
            data = value.get("data")
            if isinstance(data, dict) and isinstance(data.get("jobs"), list):
                for job in data["jobs"]:
                    sys.stdout.write("%s\t%s\t%s\n" % (
                        job.get("id", "-"), job.get("state", "UNKNOWN"),
                        job.get("name", "-")))
            else:
                sys.stdout.write(json.dumps(data, ensure_ascii=False,
                                            sort_keys=True, indent=2) + "\n")
        else:
            error = value.get("error") or {}
            sys.stdout.write("outcome: %s\nerror [%s]: %s\n" % (
                outcome, error.get("code", "error"),
                error.get("message", "operation failed")))
            if meta.get("request_id"):
                sys.stdout.write("request_id: %s\n" % meta["request_id"])
            if error.get("details") is not None:
                sys.stdout.write("details: %s\n" % error["details"])
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
        duration=args.duration, qos=args.qos, workdir=args.workdir,
        output=args.output, environment=args.environment)


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
        state = state_value(client)
        return response_envelope(True, {"gpus": state.get("gpus"), "metrics": state.get("metrics")})
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
                    month = int(args.month)
                except (TypeError, ValueError):
                    raise TimeError("invalid_arguments", "--month must be YYYYMM", status=400)
                if args.gpu_hours is None or not 1 <= args.gpu_hours <= 100000 or not 197001 <= month <= 999912 or not 1 <= month % 100 <= 12:
                    raise TimeError("invalid_arguments", "provide --gpu-hours (1..100000) and --month YYYYMM", status=400)
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
    try:
        raw = list(sys.argv[1:] if argv is None else argv)
        json_flag = False
        pretty_flag = False
        filtered = []
        command_boundary = False
        for value in raw:
            if value == "--":
                command_boundary = True
            if not command_boundary and value == "--json":
                json_flag = True
                continue
            if not command_boundary and value == "--pretty":
                pretty_flag = True
                continue
            filtered.append(value)
        args = parser.parse_args(filtered)
        args.json = bool(getattr(args, "json", False) or json_flag)
        args.pretty = bool(getattr(args, "pretty", False) or pretty_flag)
        if not args.command:
            parser_error("a command is required")
        result = handle(args)
        emit(result, args)
        outcome = result.get("meta", {}).get("outcome")
        return EXIT_UNKNOWN if outcome == "unknown" else EXIT_CONFLICT if outcome == "rejected" else EXIT_OK
    except TimeError as exc:
        emit(format_error(exc, request_id=getattr(exc, "request_id", None) or getattr(locals().get("args", None), "client_request_id", None)),
             locals().get("args"))
        return exit_code(exc)
    except (ValueError, TypeError) as exc:
        error = TimeError("invalid_arguments", str(exc), status=400)
        emit(format_error(error), locals().get("args"))
        return EXIT_ARGS
    except KeyboardInterrupt:
        error = TimeError("interrupted", "operation interrupted; no cancellation was sent", status=499)
        emit(format_error(error), locals().get("args"))
        return EXIT_UNKNOWN


if __name__ == "__main__":
    raise SystemExit(main())
