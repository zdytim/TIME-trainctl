# Evidence boundary

This artifact was implemented against the reviewed plan and the supplied
read-only snapshots. The supplied input manifest records:

| Input | SHA-256 | Use |
|---|---|---|
| `time-cli-mcp-plan.md` | `6a433edb4b167c3de80095b98bca5b801e118569b79050d9801c9bd90b6dc96c` | reviewed design and compatibility boundary |
| `pro6000d-training-api.py` | `1b13fbbb298efc4c2472eaa1ae9d3f5d9f4ec62232cf4c18542b1be41fa9c5c1` | supplied root broker API snapshot |
| `pro6000d-training-client.py` | `cc8d7d2ed43543424dcc84f7a370990373bffef0166ddbd0fb488b2863fa5b97` | supplied one-request bridge behavior |

The reviewed evidence says the broker reads a newline-delimited JSON request,
uses `SO_PEERCRED`, and exposes `/api/v1/me`, `/state`, `/jobs`, validate,
submit, cancel, log, application, preview/apply, and audit routes. It also says
the installed API has no capabilities route and no installed `monthly_quota.py`;
the staged monthly implementation is explicitly not enforcement-ready.

No runtime health, systemd active state, credentials, configuration secrets,
Slurm commands, training submission, deployment, restart, or tests were run
for this implementation. Syntax/compilation checks are the only intended
validation.
