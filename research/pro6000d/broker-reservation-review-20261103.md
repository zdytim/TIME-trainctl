# Broker reservation candidate review

Source: isolated u20 dispatch `j-b35bf592d1ce4705a09f` attempt 2, collected `changes.patch` SHA-256 verified by dispatcher manifest. This is an unmerged candidate and must not be deployed or enabled.

1. `reserve_submission()` computes `summary(uid)` before `BEGIN IMMEDIATE`; independent concurrent database connections can observe the same remaining amount and both create reservations. Move the entire read/check/write under a single serialized transaction and reconcile idempotency before the capacity check.
2. `_release_proven()` treats `scontrol show job` reporting an invalid job ID as evidence GPU resources were freed. Controller absence alone does not establish node cleanup or identity across ID reuse, and can cause premature refund. Keep the reservation and fail closed unless reliable, generation-bound resource-release evidence exists.
3. Reservations carry no selected tier or accounting month; `summary()` subtracts their total from aggregate balance instead of enforcing each selected tier. This does not implement the accepted per-tier rule and cannot safely handle month boundaries.
4. `enforcement_ready` trusts configuration and ledger status, while `summary()` reports readiness false and no atomic Slurm allocation gate exists. A configuration flag cannot establish the required pre-allocation guarantee. Do not enable or route production traffic through this candidate.

The candidate is retained in the dispatcher collection for later work; no tests were run and production was not modified. Separately, the prior controller candidate is incomplete and its failed/partial delivery must not be taken as an enabled gate.
