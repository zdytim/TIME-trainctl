# u20 user CLI request replay protection

- Source commit `a738651` (including replay protection `17622e4`), pushed to `u20-slurm19` before deployment; target `u20-Z790-S-DDR4`, user `u20`, 2026-10-30 10:59 Asia/Shanghai.
- The manifest-verified client was staged at `/home/u20/.local/share/time/candidate-a738651/`. Only `/home/u20/.local/bin/timectl` and `time-mcp` wrappers were redirected from `candidate-d7e13de`; their exact previous contents and modes were checked and backed up at `/home/u20/.local/share/time/backup-before-a738651/`. No backend, Slurm service, controller or training task was changed.
- Health: `timectl whoami` identified authenticated socket user `u20`; MCP initialized and listed 10 tools. The check did not submit or cancel work. Same-machine previously recorded request IDs are now refused for re-sending; different machines or state directories still need server-side idempotency. MCP mutation tools remain withheld. No tests were run.
- Rollback was not needed or exercised. After checking that the wrappers still reference `candidate-a738651`, restore the corresponding files from the backup. Strict monthly enforcement remains disabled.
