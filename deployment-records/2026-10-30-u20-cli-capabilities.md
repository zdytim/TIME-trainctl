# u20 CLI/MCP capability discovery candidate

- Source commit: `b695aab`; target: u20 host `u20-Z790-S-DDR4`, account `u20`; deployed 2026-10-30 10:26 Asia/Shanghai.
- Changed components: user-owned `~/.local/bin/timectl` and `time-mcp` wrappers, pointing to `~/.local/share/time/candidate-b695aab/`. This changes no root-owned backend, system service, controller or running job. The uploaded files matched the candidate manifest and passed Python AST syntax checks.
- Previous exact wrappers backed up at `~/.local/share/time/backup-before-b695aab/`, created only after ownership and exact old-wrapper checks. Original code remains at `~/.local/share/time/candidate-f20edf7/`.
- Health evidence: `timectl whoami` returned authenticated Unix user `u20`; `timectl capabilities` returned `legacy` and monthly enforcement `unknown` because the staged capabilities route is not installed on the current broker. MCP stdio initialized and listed eight read-only tools. MCP mutation tools remain disabled. No test suite, simulated workload or actual training operation was run.
- Rollback: compare current wrappers to the candidate before restoring the two backups; preserve candidate directory and existing state. Rollback was not needed and has not been exercised.
- Limits: controller remains uninstalled and disabled; this release does not enable monthly enforcement. The new broker capability endpoint is present only in the Git candidate, not u20's running backend. CLI mutations, end-to-end workflows and MCP behavior beyond the read-only startup check remain unverified.
