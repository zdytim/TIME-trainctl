# Feature matrix

This table describes the candidate source, not a claim that every feature is
installed or enabled. As recorded in the 2026-10-30 u20 deployment record,
the installed `time-mcp` wrapper removes the host-consent descriptor and
exposes the read-only tool set. The terminal-consent launcher is staged only.

| Surface | Implementation | Existing broker route | Honest limitation |
|---|---|---|---|
| Identity | `timectl whoami`, `time_identity` | `GET /api/v1/me` | Identity is the socket peer UID; request fields cannot override it. |
| Health | `timectl doctor` | `/me`, `/state` | Cannot prove service/systemd health when the broker is unreachable. |
| Queue/show | CLI and MCP | `GET /api/v1/jobs` | Prefixes must resolve to one job; visibility is broker-controlled. |
| GPU/quota | CLI and MCP state views | `GET /api/v1/state` | GPU telemetry and scheduler health can be unavailable; legacy brokers may omit usage. |
| Log tail | CLI and MCP | `GET /api/v1/jobs/{id}/log` | Existing API is a bounded tail of at most 1 MiB; follow/offset is unavailable. |
| Validate | `run --dry-run`, `validate`, `time_validate_job` | `POST /api/v1/jobs/validate` | Validation does not reserve resources or prove executable dependencies. |
| Submit | CLI `run`; staged MCP `time_submit_job` with host consent | `POST /api/v1/jobs` | Installed u20 MCP is read-only; legacy shell contract; ambiguous transport result is unknown. |
| Cancel | CLI; staged MCP `time_cancel_job` with host consent | `POST /api/v1/jobs/{id}/cancel` | Installed u20 MCP is read-only; cancellation may remain pending/unknown until reconciliation. |
| Applications | CLI read/create; staged MCP monthly creation with host consent | `GET/POST /api/v1/applications` | Monthly creation requires advertised server idempotency; temporary CLI requests require a complete real job. |
| Admin approvals | CLI; staged MCP decision and grant reconciliation with host consent | `POST /api/v1/applications/{id}/decision` | Broker administrator authorization is required; adjusted approval requires actual parameters. Installed u20 MCP is read-only. |
| Quota/rules admin | CLI preview/apply | existing preview/apply routes | MCP mutations withheld; apply requires a fresh preview and CLI consent. |
| Audit | CLI and read-only MCP | `GET /api/v1/audit` | Existing API is administrator-only and bounded to recent entries. |
| Capabilities | CLI and read-only MCP discovery | `GET /api/v1/capabilities`, fallback to `/me` on 404 | Only the new staged broker exposes this route; older deployed brokers remain legacy/unknown. Client support is reported separately. |
| Monthly quota | Client reports staged/disabled if explicitly advertised, otherwise unknown; monthly creation requires capability support | staged broker and controller candidates | The current u20 broker does not advertise monthly creation, and the controller gate is not enabled. No enforcement claim is made. |
| MCP transport | dependency-free stdio JSON-RPC | N/A | Per-user process only; the staged consent host must be trusted and requires a controlling terminal. Same-UID peers are not a security boundary. |
