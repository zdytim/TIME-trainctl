#!/usr/bin/env python3
"""User-scoped MCP stdio server for the existing time broker.

The implementation uses the MCP JSON-RPC wire contract directly, so it has no
third-party package dependency and makes no global package changes.  stdin and
stdout carry protocol messages only; diagnostics go to stderr.
"""

from __future__ import print_function

import json
import re
import sys
import traceback

from time_consent import ConsentUnavailable, inherited_host_consent
from time_core import (BrokerClient, RequestLedger, TimeError, UnknownOutcome,
                       discover_server_capabilities,
                       error_envelope, feature_capabilities,
                       job_outcome, normalize_command_job,
                       response_envelope)


PROTOCOL_VERSION = "2024-11-05"


def obj_schema(properties, required=None, additional=True):
    schema = {"type": "object", "properties": properties,
              "additionalProperties": additional}
    if required:
        schema["required"] = required
    return schema


STRING = {"type": "string"}
INTEGER = {"type": "integer", "minimum": 1}
BOOLEAN = {"type": "boolean"}
COMMAND = {"type": "array", "items": {"type": "string"}, "minItems": 1}


MUTATION_AUTH = {
    "confirm": {"type": "boolean", "const": True,
                "description": "Must be true for this specific mutation."},
    "user_intent": {"type": "string", "minLength": 1,
                    "description": "Exact action the user authorized."},
}


def job_properties(include_auth=False):
    result = {
        "command": COMMAND,
        "name": STRING,
        "gpus": INTEGER,
        "cpus": INTEGER,
        "mem": STRING,
        "time": STRING,
        "qos": STRING,
        "workdir": STRING,
        "output": STRING,
        "environment": STRING,
        "client_request_id": STRING,
        "temporary_application_id": STRING,
    }
    if include_auth:
        result.update(MUTATION_AUTH)
    return result


TOOLS = [
    {"name": "time_identity", "description": "Read identity established by Unix socket peer credentials.",
     "inputSchema": obj_schema({})},
    {"name": "time_state", "description": "Read broker-filtered state for this authenticated user.",
     "inputSchema": obj_schema({})},
    {"name": "time_gpu", "description": "Read GPU inventory and metrics from the authenticated broker state.",
     "inputSchema": obj_schema({})},
    {"name": "time_quota", "description": "Read the current user's quota and conservatively reported monthly status.",
     "inputSchema": obj_schema({})},
    {"name": "time_list_jobs", "description": "List jobs visible to this authenticated user.",
     "inputSchema": obj_schema({})},
    {"name": "time_validate_job", "description": "Validate a command through the broker without queueing it.",
     "inputSchema": obj_schema(job_properties(), required=["command"])},
    {"name": "time_submit_job", "description": "Submit one user-authorized job through the broker; never calls sbatch directly.",
     "inputSchema": obj_schema(job_properties(True), required=["command", "confirm", "user_intent"])},
    {"name": "time_get_job_log", "description": "Read at most the existing broker's bounded log tail; follow is unsupported.",
     "inputSchema": obj_schema({"job_id": STRING}, required=["job_id"])},
    {"name": "time_cancel_job", "description": "Cancel one specific user-authorized job through the broker.",
     "inputSchema": obj_schema({"job_id": STRING, **MUTATION_AUTH},
                                required=["job_id", "confirm", "user_intent"])},
    {"name": "time_list_requests", "description": "List broker resource applications visible to this user.",
     "inputSchema": obj_schema({})},
    {"name": "time_create_request", "description": "Create a user-authorized temporary or quota application; monthly capability is unknown and this mutation is withheld.",
     "inputSchema": obj_schema({
         "kind": {"type": "string", "enum": ["temporary_job", "quota_change", "monthly_gpu_hours"]},
         "requested": {"type": "object"}, "reason": STRING,
         **MUTATION_AUTH}, required=["kind", "reason", "confirm", "user_intent"])},
    {"name": "time_decide_request", "description": "Make an explicit administrator-authorized application decision.",
     "inputSchema": obj_schema({
         "application_id": STRING,
         "action": {"type": "string", "enum": ["approve", "approve_adjusted", "reject"]},
         "decision_reason": STRING, "approved": {"type": "object"},
         "valid_minutes": INTEGER, **MUTATION_AUTH},
         required=["application_id", "action", "confirm", "user_intent"])},
    {"name": "time_preview_quota", "description": "Preview an existing administrator quota change route.",
     "inputSchema": obj_schema({"scope": {"type": "string", "enum": ["default", "user"]},
                                 "uid": INTEGER, "quota": {"type": "object"},
                                 "inherit": BOOLEAN})},
    {"name": "time_apply_quota", "description": "Apply one fresh quota preview after explicit administrator authorization.",
     "inputSchema": obj_schema({"preview_id": STRING, **MUTATION_AUTH},
                                required=["preview_id", "confirm", "user_intent"])},
    {"name": "time_preview_rules", "description": "Preview an existing administrator rules change route.",
     "inputSchema": obj_schema({"rules": {"type": "object"}}, required=["rules"])},
    {"name": "time_apply_rules", "description": "Apply one fresh rules preview after explicit administrator authorization.",
     "inputSchema": obj_schema({"preview_id": STRING, **MUTATION_AUTH},
                                required=["preview_id", "confirm", "user_intent"])},
    {"name": "time_read_audit", "description": "Read the existing administrator audit route.",
     "inputSchema": obj_schema({})},
    {"name": "time_capabilities", "description": "Discover the broker capability contract or report legacy/unknown status.",
     "inputSchema": obj_schema({})},
]


MUTATION_TOOL_NAMES = {
    "time_submit_job", "time_cancel_job", "time_create_request",
    "time_decide_request", "time_apply_quota", "time_apply_rules",
    "time_preview_quota", "time_preview_rules",
}

# Only submit and cancel have a staged host-consent adapter. Other mutation
# paths remain absent until their own API and consent contracts are ready.
HOST_CONSENT_TOOLS = {"time_submit_job", "time_cancel_job"}


def require_intent(args, expected):
    if args.get("confirm") is not True or args.get("user_intent") != expected:
        raise TimeError("confirmation_required",
                        "mutation requires confirm=true and user_intent=%s" % expected,
                        status=400)


def job_from_args(args):
    command = args.get("command")
    if not isinstance(command, list) or not command or any(not isinstance(x, str) for x in command):
        raise TimeError("invalid_arguments", "command must be a non-empty string array", status=400)
    try:
        return normalize_command_job(
            command, name=args.get("name"), gpus=args.get("gpus", 1),
            cpus=args.get("cpus", 1), memory=args.get("mem", "1GiB"),
            duration=args.get("time", "60m"), qos=args.get("qos"),
            workdir=args.get("workdir"), output=args.get("output"),
            environment=args.get("environment", "system"))
    except ValueError as exc:
        raise TimeError("invalid_arguments", str(exc), status=400)


class Server(object):
    def __init__(self):
        self.client = BrokerClient()
        self.consent = inherited_host_consent()
        self.ledger = RequestLedger() if self.consent else None

    def tools(self):
        allowed = HOST_CONSENT_TOOLS if self.consent else set()
        return [tool for tool in TOOLS if tool["name"] not in MUTATION_TOOL_NAMES or tool["name"] in allowed]

    def read(self, method, path, body=None, timeout=10):
        return self.client.request(method, path, body, timeout=timeout, mutation=False)

    def mutate(self, method, path, body, intent, timeout=15, request_id=None):
        if not self.consent or not self.ledger:
            raise TimeError("feature_not_enabled", "host consent channel is unavailable", status=409)
        require_intent(body, intent)
        request_id = request_id or self.ledger.new_id()
        payload = dict(body)
        payload.pop("confirm", None)
        payload.pop("user_intent", None)
        try:
            approved = self.consent.authorize(method, path, payload)
        except ConsentUnavailable as exc:
            self.consent = None
            raise TimeError("consent_unavailable", str(exc), status=503)
        if not approved:
            raise TimeError("confirmation_declined", "host did not approve this action", status=403)
        self.ledger.reserve(request_id, method, path, payload)
        try:
            result = self.client.request(method, path, payload, timeout=timeout,
                                         mutation=True)
        except TimeError as exc:
            exc.request_id = request_id
            try:
                self.ledger.update(request_id, outcome=exc.outcome, error=error_envelope(exc))
            except (TimeError, OSError):
                pass
            raise
        outcome = job_outcome(result.get("job"), True) if isinstance(result, dict) and "job" in result else "confirmed"
        try:
            self.ledger.update(request_id, outcome=outcome, result=result)
        except (TimeError, OSError) as exc:
            error = UnknownOutcome("broker replied but local request state could not be recorded", details=str(exc))
            error.request_id = request_id
            raise error
        return result, request_id, outcome

    def call(self, name, args):
        args = args if isinstance(args, dict) else {}
        if name in MUTATION_TOOL_NAMES and (name not in HOST_CONSENT_TOOLS or not self.consent):
            raise TimeError("feature_not_enabled", "host-authorized mutation is unavailable", status=409)
        if name == "time_identity":
            return response_envelope(True, self.read("GET", "/api/v1/me"))
        if name == "time_state":
            return response_envelope(True, self.read("GET", "/api/v1/state"))
        if name == "time_gpu":
            state = self.read("GET", "/api/v1/state")
            return response_envelope(True, {"gpus": state.get("gpus"), "metrics": state.get("metrics")})
        if name == "time_quota":
            state = self.read("GET", "/api/v1/state")
            discovery = discover_server_capabilities(self.client)
            return response_envelope(True, {
                "quota": state.get("quota"), "usage": state.get("usage"),
                "monthly_quota": state.get("monthly_quota", {
                    "state": "unknown", "enforcement_ready": "unknown",
                    "reason": "legacy state response has no monthly quota field"}),
                "monthly_enforcement": feature_capabilities(discovery)["monthly_quota"]})
        if name == "time_list_jobs":
            return response_envelope(True, self.read("GET", "/api/v1/jobs"))
        if name == "time_validate_job":
            return response_envelope(True, self.read("POST", "/api/v1/jobs/validate", job_from_args(args), timeout=10))
        if name == "time_submit_job":
            require_intent(args, "submit_job")
            job = job_from_args(args)
            request_id = args.get("client_request_id") or self.ledger.new_id()
            job["client_request_id"] = request_id
            if args.get("temporary_application_id"):
                job["temporary_application_id"] = args["temporary_application_id"]
            data, _, outcome = self.mutate("POST", "/api/v1/jobs", {
                **job, "confirm": args.get("confirm"),
                "user_intent": args.get("user_intent")}, "submit_job", 45,
                request_id=request_id)
            return response_envelope(True, data, request_id=request_id, outcome=outcome)
        if name == "time_get_job_log":
            job_id = require_identifier(args, "job_id")
            data = self.read("GET", "/api/v1/jobs/%s/log" % job_id, timeout=5)
            data["tail_limit_bytes"] = 1048576
            data["follow"] = False
            return response_envelope(True, data)
        if name == "time_cancel_job":
            job_id = require_identifier(args, "job_id")
            data, request_id, outcome = self.mutate(
                "POST", "/api/v1/jobs/%s/cancel" % job_id, args,
                "cancel_job", 15)
            return response_envelope(True, data, request_id=request_id, outcome=outcome)
        if name == "time_list_requests":
            return response_envelope(True, self.read("GET", "/api/v1/applications"))
        if name == "time_create_request":
            require_intent(args, "create_request")
            if args.get("kind") == "monthly_gpu_hours":
                raise TimeError("feature_not_enabled", "monthly quota enforcement is disabled; monthly applications are unavailable", status=409)
            body = {"kind": args.get("kind"), "requested": args.get("requested", {}),
                    "reason": args.get("reason", "")}
            data, request_id, outcome = self.mutate(
                "POST", "/api/v1/applications", {
                    **body, "confirm": args.get("confirm"),
                    "user_intent": args.get("user_intent")},
                "create_request", 15)
            return response_envelope(True, data, request_id=request_id, outcome=outcome)
        if name == "time_decide_request":
            require_intent(args, "decide_request")
            body = {key: args[key] for key in (
                "action", "decision_reason", "approved", "valid_minutes") if key in args}
            body.update({"confirm": args.get("confirm"), "user_intent": args.get("user_intent")})
            data, request_id, outcome = self.mutate(
                "POST", "/api/v1/applications/%s/decision" % require_identifier(args, "application_id"),
                body, "decide_request", 15)
            return response_envelope(True, data, request_id=request_id, outcome=outcome)
        if name == "time_preview_quota":
            body = {key: args[key] for key in ("scope", "uid", "quota", "inherit") if key in args}
            return response_envelope(True, self.read("POST", "/api/v1/quotas/preview", body))
        if name == "time_apply_quota":
            data, request_id, outcome = self.mutate(
                "POST", "/api/v1/quotas/apply",
                {"preview_id": require_identifier(args, "preview_id"),
                 "confirm": args.get("confirm"), "user_intent": args.get("user_intent")},
                "apply_quota", 15)
            return response_envelope(True, data, request_id=request_id, outcome=outcome)
        if name == "time_preview_rules":
            if not isinstance(args.get("rules"), dict):
                raise TimeError("invalid_arguments", "rules must be an object", status=400)
            return response_envelope(True, self.read("POST", "/api/v1/rules/preview", {"rules": args["rules"]}))
        if name == "time_apply_rules":
            data, request_id, outcome = self.mutate(
                "POST", "/api/v1/rules/apply",
                {"preview_id": require_identifier(args, "preview_id"),
                 "confirm": args.get("confirm"), "user_intent": args.get("user_intent")},
                "apply_rules", 15)
            return response_envelope(True, data, request_id=request_id, outcome=outcome)
        if name == "time_read_audit":
            return response_envelope(True, self.read("GET", "/api/v1/audit"))
        if name == "time_capabilities":
            return response_envelope(True, feature_capabilities(discover_server_capabilities(self.client)))
        raise TimeError("unknown_tool", "unknown MCP tool: %s" % name, status=404)


def require_string(args, key):
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise TimeError("invalid_arguments", "%s must be a non-empty string" % key, status=400)
    return value


def require_identifier(args, key):
    value = require_string(args, key)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise TimeError("invalid_arguments", "%s contains invalid identifier characters" % key, status=400)
    return value


def reply(request_id, result=None, error=None):
    message = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def tool_result(envelope):
    text = json.dumps(envelope, ensure_ascii=False, sort_keys=True)
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": envelope,
            "isError": not envelope.get("ok", False)}


def main():
    server = Server()
    for line in sys.stdin:
        if not line.strip():
            continue
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            method = request.get("method")
            if method == "notifications/initialized" or method == "notifications/cancelled":
                continue
            if method == "initialize":
                reply(request_id, {"protocolVersion": PROTOCOL_VERSION,
                                   "capabilities": {"tools": {"listChanged": False}},
                                   "serverInfo": {"name": "time-mcp", "version": "0.1.0"},
                                   "instructions": "User-scoped time tools. Mutations require a trusted host consent channel."})
                continue
            if method == "ping":
                reply(request_id, {})
                continue
            if method == "tools/list":
                reply(request_id, {"tools": server.tools()})
                continue
            if method == "tools/call":
                params = request.get("params") or {}
                name = params.get("name")
                try:
                    envelope = server.call(name, params.get("arguments") or {})
                    reply(request_id, tool_result(envelope))
                except TimeError as exc:
                    reply(request_id, tool_result(error_envelope(exc, request_id=getattr(exc, "request_id", None))))
                continue
            reply(request_id, error={"code": -32601, "message": "method not found"})
        except (ValueError, TypeError) as exc:
            if request_id is None:
                reply(None, error={"code": -32700, "message": "invalid JSON-RPC request"})
            else:
                reply(request_id, error={"code": -32600, "message": str(exc)})
        except Exception as exc:
            print("time-mcp internal error: %s" % exc, file=sys.stderr)
            print(traceback.format_exc(), file=sys.stderr)
            if request_id is not None:
                reply(request_id, error={"code": -32603, "message": "internal error"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
