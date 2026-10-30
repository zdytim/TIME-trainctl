# time CLI and MCP artifact

The platform brand remains **time**. The executable is **`timectl`** so the
shell keyword and `/usr/bin/time` are never overwritten. This artifact uses the
existing root-owned Unix-socket broker as the current Unix user. It does not
call `sbatch`, `scancel`, SQLite, `sudo`, or an administrator gateway.

Install for the ordinary login user with `./install.sh`. The installer refuses
root and refuses prefixes outside that user's home directory. It will not
overwrite an existing `timectl`, `time-mcp`, or helper path. It changes only a
user-owned prefix and records a hash-checked manifest. `./uninstall.sh` and
`./rollback.sh` safely remove only files still matching that manifest. They do
not touch the backend, Slurm, systemd, server versions, or credentials.

Use `timectl --help` after installation. Typical commands are:

```text
timectl whoami
timectl doctor
timectl capabilities
timectl queue
timectl show JOB_ID
timectl gpu
timectl quota
timectl logs JOB_ID
timectl run --gpus 1 --mem 32GiB --time 12h --yes -- python -u train.py
timectl run --dry-run --gpus 1 -- python -u train.py
timectl cancel JOB_ID --yes
```

The command after `--` is shell-quoted into the legacy backend's `command`
field. The backend still invokes Bash; this client does not claim native
`execve` argv semantics. Pipes and shell operators require an explicit command
such as `bash -lc '...'`.

The MCP server is `time-mcp`. It is a per-user stdio process. Configure the
host to start it as the login user; do not run one shared root process and do
not pass a UID as a tool argument. Its stdout is JSON-RPC only and its stderr
is diagnostic output. The delivered MCP tool list is read-only. Mutation tools
are intentionally withheld: `confirm` and `user_intent` supplied by an AI
caller are not independent human authorization. A future host integration must
provide a one-shot, action-bound consent token out of band before mutations can
be enabled.

CLI mutations require `--yes` in automation and ask for `y/N` confirmation on
a TTY. A submit request is written to a locked 0700/0600 user-local ledger
before sending. A transport timeout or empty, malformed, or oversized broker
response returns
`outcome: "unknown"`, exit code 6, and the durable request ID; it is never
automatically retried. `timectl request-status REQUEST_ID` checks visible jobs
by the same broker request ID.

Temporary approval requests require the complete real job after `--`; quota
change requests require all three quota limits. Adjusted approvals require a
non-empty `--approved-json` object. The client refuses fake placeholder jobs
and empty quota payloads.

The staged backend exposes an additive capability endpoint; both CLI and MCP
use the same discovery logic and fall back to legacy identity on a 404. Until
the endpoint is deployed, older brokers remain legacy/unknown. The client
recognizes only explicit disabled or staged monthly status and does not enable
monthly operations or claim pre-allocation enforcement.

Staged monthly request support requires a server explicitly advertising the
`owner-request-id-v1` contract and `available: true`; legacy and disabled
servers reject the request locally. Example after an independently validated
server is enabled: `timectl request create --kind monthly_gpu_hours --gpu-hours
10 --month 202610 --reason "project requires additional capacity" --yes`.
An administrator can run `timectl admin request reconcile APPLICATION_ID
--yes` for a previously approved, unconfirmed controller grant. These commands
are not evidence that monthly enforcement is active.
