# Staged broker-to-controller quota client

`controller_bridge.py` is a root-only broker client for the candidate local controller socket. It checks the socket and parent ownership/modes and Linux peer credentials, limits responses, validates the fixed snapshot fields, and exposes a grant call that treats transport/protocol failures after send as an unknown outcome. The broker must derive identity from its existing Unix peer authentication, enforce administrator permissions, persist the approval and audit, and reconcile using the identical grant ID and fields; this module does not grant authorization by itself.

`u20-broker-controller-read.patch` applies after `u20-backend-idempotency.patch` against runtime baseline `65d213100a0e0ebd3f9fe87b51d1625f8539acf954ca572b43fa48a07c43173c`. With explicit `monthly_controller.read_enabled`, `/api/v1/state` adds the current authenticated user's controller snapshot. If disabled or unavailable it reports unknown, not zero. It never reports monthly enforcement verified. Existing CLI and MCP quota reads consume that state field. No grant endpoint or approval integration is added. The new Python module must be installed with the broker if the patch is ever deployed.

This is staged code only; no tests, socket interactions, database migrations, or service restarts were performed. The live backend and disabled quota gate remain unchanged. Response protocol, credential checks, power-loss and concurrent approval behavior still need validation before use.

On 2026-10-30, `controller_bridge.py` from `c30fe26` was copied into the u20 isolated build directory and parsed successfully with Python AST. The three running Slurm/training services reported active. The broker patch remains uninstalled and no bridge connection or grant was attempted.
