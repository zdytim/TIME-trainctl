# Stable monthly tier snapshot candidate

`tier-snapshot.patch` follows `d4f0371` and evaluates a list of users against one supplied timestamp while holding the ledger lock once. On error the output array is unchanged; a snapshot is read-only and does not allocate GPUs. The existing single-user helper delegates to it.

This does not connect tiers to scheduling. A scheduler must capture a fresh snapshot before sorting, define precedence against Slurm reservations and preemption, avoid mutable comparisons, cover the builtin FIFO fast path and backfill, and recheck admission at allocation. A snapshot can become stale as jobs start or finish; it is not an admission authorization. Ledger rollback detection, authenticated grant transport and the deployed binary/ABI also remain unresolved. Keep the gate disabled. No tests or training jobs were run.

On the u20 isolated source, the preceding `d4f0371` monthly_quota.c passed `gcc -std=gnu99 -I. -fsyntax-only` on 2026-10-30. This is a syntax check, not a build, behavior validation or deployment.

On 2026-10-30 the committed `eb0fd27` patch applied to the isolated u20 source and `gcc -std=gnu99 -I. -fsyntax-only src/slurmctld/monthly_quota.c` exited 0. It was not linked to Slurm or enabled.
