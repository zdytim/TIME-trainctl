# Staged request ledger directory durability

The CLI and consent-enabled MCP record a request before sending it. The candidate already fsynced the temporary file, but did not fsync its containing directory after atomic replacement; a crash could lose the directory entry despite a completed write. Newly created state directories also lacked explicit parent-directory synchronization.

The candidate now synchronizes parent directories after creating missing path components and synchronizes the ledger directory after replacement. Any sync failure propagates before a new request is sent; if an update fails after the broker has responded, the existing caller reports an unknown outcome. This improves the intended crash-recovery ordering but is not proof of durability on every filesystem, storage device or failure mode. The user must still reconcile unknown outcomes rather than blindly repeat a mutation.

Only source inspection and Python AST parsing were performed. No crash simulation, behavioral test, installation, live mutation or service restart was performed. The installed u20 CLI/MCP remains at its previously recorded version.
