# Feature matrix

| Surface | Implementation | Existing broker route | Honest limitation |
|---|---|---|---|
| Identity | `timectl whoami`, `time_identity` | `GET /api/v1/me` | Identity is the socket peer UID; request fields cannot override it. |
| Health | `timectl doctor` | `/me`, `/state` | Cannot prove service/systemd health when the broker is unreachable. |
| Queue/show | CLI and MCP | `GET /api/v1/jobs` | Prefixes must resolve to one job; visibility is broker-controlled. |
| GPU/quota | CLI state views | `GET /api/v1/state` | GPU telemetry and scheduler health can be unavailable. |
| Log tail | CLI and MCP | `GET /api/v1/jobs/{id}/log` | Existing API is a bounded tail of at most 1 MiB; follow/offset is unavailable. |
| Validate | `run --dry-run`, `validate`, `time_validate_job` | `POST /api/v1/jobs/validate` | Validation does not reserve resources or prove executable dependencies. |
| Submit | CLI `run` only | `POST /api/v1/jobs` | MCP mutation withheld; legacy shell contract; ambiguous transport result is unknown. |
| Cancel | CLI only | `POST /api/v1/jobs/{id}/cancel` | MCP mutation withheld; cancellation may remain pending/unknown until reconciliation. |
| Applications | CLI read/create | `GET/POST /api/v1/applications` | MCP mutations withheld; temporary requests require a complete real job. |
| Admin approvals | CLI only | `POST /api/v1/applications/{id}/decision` | MCP mutations withheld; adjusted CLI approval requires actual parameters. |
| Quota/rules admin | CLI preview/apply | existing preview/apply routes | MCP mutations withheld; apply requires a fresh preview and CLI consent. |
| Audit | CLI and read-only MCP | `GET /api/v1/audit` | Existing API is administrator-only and bounded to recent entries. |
| Capabilities | CLI and read-only MCP discovery | `GET /api/v1/capabilities`, fallback to `/me` on 404 | Only the new staged broker exposes this route; older deployed brokers remain legacy/unknown. Client support is reported separately. |
| Monthly quota | Staged/disabled if explicitly reported; otherwise unknown, client refusal | no discovered authoritative controller | No monthly request or enforcement claim is made. |
| MCP transport | dependency-free stdio JSON-RPC | N/A | Per-user process only; read-only tools until independent consent exists. |
