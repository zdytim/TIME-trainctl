# Static review repair audit

This audit records the independent review findings and the corresponding
repair evidence. It does not claim runtime behavior because no backend,
deployment, job, or test execution was authorized.

| Concern | Repair | Evidence |
|---|---|---|
| Empty, malformed, or oversized mutation responses could look rejected | Added `_MalformedResponse`; after a mutation is sent, empty, invalid JSON, non-object, invalid envelope, malformed rejection, and response-over-limit cases raise `UnknownOutcome`. A complete broker `ok:false` with code/message remains a rejected result; `submission_uncertain` is unknown. | `time_core.py`: `_MalformedResponse`, `BrokerClient.request`, `TimeError.outcome`. |
| Ledger had TOCTOU and no concurrent reservation lock | Added per-user `fcntl.flock` transaction around reserve/update/read; reservation compares and writes under one lock, with atomic `os.replace`. | `time_core.py`: `RequestLedger._locked`, `reserve`, `_write_unlocked`. |
| Ledger path/owner/symlink protection was incomplete | State directory, lock, and ledger require current-user ownership; existing components and files cannot be symlinks; modes are tightened to 0700/0600; stored request hash is recomputed before reuse. | `time_core.py`: `_assert_no_symlink_components`, `_check_owned_regular`, `_ensure_directory`. |
| Temporary approval request used fake `requested-job`; quota request could be empty | Temporary CLI requests require a complete command and normalize the real job fields; quota requests require all limits and validate their range/relation. | `timectl.py`: request-create parser and `handle`. |
| Adjusted approval could omit actual approved parameters | `--adjusted` now requires non-empty `--approved-json` and sends that object. | `timectl.py`: admin request parser and `handle_admin`. |
| Installer could delete arbitrary manifest paths | Manifest cleanup is restricted to the current user's home, fixed managed manifest path, fixed release directory, exact expected file set, current-user-owned non-symlink regular files, and matching hashes. | `installer.py`: `user_prefix`, `load_manifest`, `owned_regular`, `remove_installed`. |
| MCP `confirm`/`user_intent` were AI-supplied | Submit/cancel/create/decide/preview/apply mutation tools are removed from `tools/list`; direct mutation names fail closed with `feature_not_enabled`. A host-issued out-of-band consent design is documented as a separate upgrade. | `time_mcp.py`: `MUTATION_TOOL_NAMES`, filtered `TOOLS`, `Server.call`; `docs/API-UPGRADES.md`. |
| Capabilities hard-coded server true/disabled values | Capability output now reports `server_state: unknown`, reviewed route evidence, client support, and runtime `/me` discovery separately. Monthly enforcement is unknown and client requests are refused. | `time_core.py`: `feature_capabilities`; CLI/MCP discovery paths. |
| Temporary authorization field confusion | Submission continues to send only the broker's `temporary_application_id`; request creation no longer fabricates a placeholder job. | `timectl.py`, `time_mcp.py`. |
| TTY output and confirmations were weak | TTY defaults to human-readable output; `--json` works before or after the command; automation requires `--yes`; TTY mutations prompt `y/N`; rejected and unknown outcomes are shown. | `timectl.py`: `emit`, `mutation_allowed`, `main`, `exit_code`. |

Validation was limited to source compilation/import and shell syntax checks.
No runtime broker, installer mutation, MCP session, deployment, restart,
training submission, or host-service change was performed.
