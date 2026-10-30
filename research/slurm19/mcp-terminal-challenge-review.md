# Terminal consent challenge hardening

The staged terminal host now writes the entire escaped action before requesting approval and requires a complete line containing `YES` plus that action's fresh request ID. A delayed response to an expired prompt cannot match a later prompt's different nonce. Output errors, incomplete lines, timeouts and malformed requests fail closed. This does not protect against arbitrary malicious processes sharing the same account/terminal; the launcher still depends on a trustworthy controlling terminal and human review.

The source passed local Python AST parsing. No terminal prompt or behavior test was run, and the current MCP installation remains unchanged.

On 2026-10-30, `0858424` was copied into u20's isolated build directory and its Python files passed AST parsing; the Slurm and training services reported active. The existing MCP wrapper was not modified.
