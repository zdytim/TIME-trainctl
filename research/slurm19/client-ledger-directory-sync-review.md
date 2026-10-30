# Staged request ledger directory durability

The CLI and consent-enabled MCP record a request before sending it. The candidate already fsynced the temporary file, but did not fsync its containing directory after atomic replacement; a crash could lose the directory entry despite a completed write. Newly created state directories also lacked explicit parent-directory synchronization.

The candidate now synchronizes directory ancestors even when they already exist, so a retry after a failed sync does not silently skip them, and synchronizes the ledger directory after replacement. Any sync failure propagates before a new request is sent; if an update fails after the broker has responded, the existing caller reports an unknown outcome. Directory sync must be supported and accessible for every ancestor, which can make unusual filesystems or restrictive parent permissions unavailable; that compatibility remains unverified. This improves the intended crash-recovery ordering but is not proof of durability on every filesystem, storage device or failure mode. The user must still reconcile unknown outcomes rather than blindly repeat a mutation.

Only source inspection and Python AST parsing were performed. No crash simulation, behavioral test, installation, live mutation or service restart was performed. The installed u20 CLI/MCP remains at its previously recorded version.

The committed source at `22d38e3` was exported to `/home/u20/.local/share/time/builds/cli-mcp-22d38e3/time-cli-mcp-review-work`. All 16 listed artifact hashes matched the manifest and all top-level Python files parsed on u20. This is a staged source artifact, not a runtime or durability check and not an installation.
