# Design B — conservative reservation and fail-closed guard

## Candidate guard

For a single node, serialize all new GPU starts through one controller guard. At the guard sample, compute:

`residual = monthly_limit + durable_topups - settled_month_usage - charge_of_all_verified_running_GPUs(now)`.

Let `G_run` be the verified GPU count of every running, configuring, and completing allocation, and let `D` be a proven upper bound from the guard sample to the physical GRES commit. Reserve `G_run * D` nanoseconds plus the new request's commit overhead. Admit only if the residual remains strictly positive after that reservation. Use monotonic time for `D`, Beijing-normalized realtime for the month, and a durable realtime fence. Any unknown running job, missing TRES map, clock regression, WAL failure, or expired token rejects the new start.

The reservation WAL would use `RESERVE`, `COMMIT`, `ABORT`, and `UNKNOWN` records keyed by an operation ID. Recovery treats a reservation without a verified resource outcome as UNKNOWN. Existing jobs continue and may create overage; the guard never kills or preempts them.

## Why the bound is not available in current Slurm19

`D` must include scheduler lock wait, scheduler interruption, fsync/checkpoint latency, preemption, burst-buffer callbacks, select callbacks, GRES plugin work, node-state publication, and any retry. The current source supplies no finite bound. `fsync` is performed by the ledger while its mutex is held, and plugin calls can block on their own locks. A timer cannot repair a transaction that has already mutated GRES or burst-buffer state: expiry would require rollback, which is the missing proof.

A guard placed before `_preempt_jobs` avoids preemption side effects for a denied request, but it still cannot prove the physical commit race unless every resource mutator waits on the same gate. A guard placed at the current adapter point (`select_nodes` after preemption) is already too late. A guard in `select/cons_tres` misses burst-buffer and node-state effects.

The only strictly safe reservation variant available without new hooks is **deny all GPU allocations while the feature is enabled**. It has no positive admission path and therefore cannot implement the requested quota service. The next conservative variant requires an enforceable finite `D`, a global resource gate, and a rollback contract; none is established by Slurm19.

## Proof obligations

1. Enumerate all current GPUs from restored job records and verified allocated TRES; unknown is not CPU zero.
2. Prove one gate covers main scheduling, backfill (`backfill.c:2717-2758`), immediate allocations, requeue, resize, recovery rebuild, and preemption.
3. Measure and enforce a hard upper bound for every operation before physical mutation. If a deadline expires, rollback must be complete before the gate is released.
4. Serialize charge sampling with the actual physical commit; no other scheduler or plugin can consume a GPU between those points.
5. Persist reservation before side effects and recover every crash point. A missing journal or clock fence freezes the system.
6. Verify month boundaries using Beijing time and reject realtime regressions.
7. Release only after trusted device cleanup and final epilog evidence. Slurm19's ordinary epilog callback is insufficient.

The existing source cannot satisfy items 2–5. Therefore this design remains a documented disabled prototype, not an enforcement implementation.
