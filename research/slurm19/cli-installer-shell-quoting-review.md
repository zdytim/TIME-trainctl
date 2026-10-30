# CLI/MCP installer shell quoting

The candidate installer generated shell wrappers using Python `repr()` for paths. Python string-literal quoting is not a shell escaping contract: a path containing an apostrophe can be rendered with double quotes, leaving shell expansion characters active. The wrapper generator now uses `shlex.quote()` for both executable and manifest paths, so they are expressed as shell arguments rather than Python literals. The artifact manifest hash was updated accordingly.

This is a staged source fix. Python AST parsing and diff checks were performed; no installer, shell wrapper, client, test or workload was executed. The existing u20 wrappers were not modified, and upgrading them still requires the migration described in `u20-cli-mcp-upgrade-review.md`.
