# Staged request ledger directory durability

The CLI and consent-enabled MCP record a request before sending it. The candidate already fsynced the temporary file, but did not fsync its containing directory after atomic replacement; a crash could lose the directory entry despite a completed write. Newly created state directories also lacked explicit parent-directory synchronization.

The candidate now synchronizes directory ancestors even when they already exist, so a retry after a failed sync does not silently skip them, and synchronizes the ledger directory after replacement. Any sync failure propagates before a new request is sent; if an update fails after the broker has responded, the existing caller reports an unknown outcome. Directory sync must be supported and accessible for every ancestor, which can make unusual filesystems or restrictive parent permissions unavailable; that compatibility remains unverified. This improves the intended crash-recovery ordering but is not proof of durability on every filesystem, storage device or failure mode. The user must still reconcile unknown outcomes rather than blindly repeat a mutation.

Only source inspection and Python AST parsing were performed. No crash simulation, behavioral test, installation, live mutation or service restart was performed. The installed u20 CLI/MCP remains at its previously recorded version.

The committed source at `22d38e3` was exported to `/home/u20/.local/share/time/builds/cli-mcp-22d38e3/time-cli-mcp-review-work`. All 16 listed artifact hashes matched the manifest and all top-level Python files parsed on u20. This is a staged source artifact, not a runtime or durability check and not an installation.

Follow-up source changes convert directory synchronization errors into structured client errors. The staged consent-enabled MCP falls back to read-only when initializing its local request ledger fails, and reports the reason on stderr; it must not expose mutation tools without a usable ledger. Failure after a broker reply remains an unknown outcome. These error paths have not been behavior-tested.

An additional staged change handles ordinary directory creation, locking and write errors as structured request-state failures. When reservation fails, CLI and MCP attach the local request ID to the error before any broker mutation. The same ID may already have a local record and should be inspected rather than blindly reused. Unusual filesystem and failure cases remain untested.

The staged CLI now opens the local ledger only for mutations and `request-status`, so an unavailable local ledger does not itself prevent identity, GPU, queue, log, validation or audit reads. The broker can still independently reject or fail those reads. This command classification is a source-level change, not behavior-tested.
