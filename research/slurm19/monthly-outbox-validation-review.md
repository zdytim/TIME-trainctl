# Prepared monthly outbox validation source

`u20-training-platform/validation/test_monthly_outbox.py` prepares focused checks of the candidate broker's `finish_monthly_grant` using only SQLite `:memory:` and an in-process fake controller. It covers a lost controller response, database failure after controller acknowledgement, same-ID/same-actor retries, and a non-admin attempt. The fake explicitly assumes controller-side ID idempotency; it cannot prove the real C controller, transport, GPU allocation or crash durability. It loads an explicitly selected assembled candidate and never constructs the live service or connects to a real socket/database.

The source was only parsed with Python AST. No test has run; the existing AGENTS.md test restriction and pending user authorization still apply. This file must be reviewed with the actual candidate and run only inside the previously described isolated environment after explicit authorization.
