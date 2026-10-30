# Staged broker peer authentication

The candidate CLI/MCP connected to a fixed local socket but did not validate the listener's peer credentials before sending commands. The staged client now requires Linux `SO_PEERCRED` to identify a live root-owned broker process before transmitting. Failure returns service unavailable without sending the request; the pre-reserved local record may still require inspection. This is consistent with u20's read-only observed service configuration (`User=root`) and root-owned socket directory on 2026-10-30.

The check does not distinguish two root processes, validate a service executable, or replace server-side caller authorization. It requires Linux peer-credential support; non-root fake brokers and other operating systems cannot satisfy the production check without an appropriately isolated validation harness. No test, server mutation, or deployment was performed.
