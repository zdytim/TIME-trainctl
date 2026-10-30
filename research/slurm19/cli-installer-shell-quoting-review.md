# CLI/MCP installer shell quoting

The candidate installer generated shell wrappers using Python `repr()` for paths. Python string-literal quoting is not a shell escaping contract: a path containing an apostrophe can be rendered with double quotes, leaving shell expansion characters active. The wrapper generator now uses `shlex.quote()` for both executable and manifest paths, so they are expressed as shell arguments rather than Python literals. The artifact manifest hash was updated accordingly.

This is a staged source fix. Python AST parsing and diff checks were performed; no installer, shell wrapper, client, test or workload was executed. The existing u20 wrappers were not modified, and upgrading them still requires the migration described in `u20-cli-mcp-upgrade-review.md`.

Committed revision `3e61e9eaaf68810eb127acb17eb69c78014dee9f` was separately checked out on u20 at `/home/u20/.local/share/time/builds/repo-3e61e9e`; all 16 CLI/MCP manifest hashes matched and the top-level Python files parsed. No installer or candidate process was executed. Slurm and the existing training service reported active at inspection. This revision is source-staged, not deployed; the earlier matching controller and broker builds remain at `2f57b32` and this change only affects the client installer and its manifest.
