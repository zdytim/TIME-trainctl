# Allocation design alternatives

## Required proof

For a strict monthly controller, the decision must linearize at the physical GPU commitment. Every path that can consume a GPU must either participate in the same transaction or be rejected. The transaction must report the actual allocated count, persist a durable intent/commit record, and provide a recoverable outcome after a controller crash. A failure after partial select, GRES, burst-buffer, preemption, or node-state mutation must have a proven rollback or become a durable UNKNOWN that blocks future admissions.

## Candidate designs

1. **Admission before selection.** The attempt19/20 adapter can persist a `START_INTENT` before select/GRES work. This protects against an unrecorded start only when the later operation is known to be all-or-nothing. Slurm 19 does not provide that guarantee: preemption is earlier, burst-buffer and select callbacks can mutate state, and the common select ABI has no transaction token. This design is rejected for strict enablement.

2. **Commit after selection.** The actual TRES count is available after `job_set_alloc_tres`, but resource consumption has already happened. A durable commit failure or scheduler crash leaves a physical allocation without a quota commit. `bb_g_job_revoke_alloc` explicitly does not release previously allocated resources. This is useful for reconciliation but cannot be the admission proof.

3. **Hold the ledger lock across selection and synchronous WAL.** This serializes the controller-local ledger but does not serialize plugin or independent scheduler resource consumers. It also puts `fsync` and checkpoint rename under the scheduling path, with no bounded latency contract. It is unsafe as a strict proof and can stall scheduling.

4. **Core transaction API.** Add a Slurm core transaction that begins before preemption and burst-buffer/select side effects, reserves the exact GRES set, commits the ledger and resource state together, and exposes a durable rollback/recovery result. The active select plugin, GRES plugin, burst-buffer contexts, preemption, requeue, resize, and node failure paths must implement it. This is the technically viable direction, but it is beyond the Slurm 19 adapter ABI and requires behavioral validation.

5. **External resource broker or device admission layer.** Move the authoritative gate into a broker that owns GPU allocation and cgroup/device cleanup, then make Slurm consume broker leases. This could supply a physical commit and cleanup proof, but it changes the resource architecture and is not present in the verified u20 Slurm 19 deployment facts.

Until one of designs 4 or 5 is implemented and behaviorally validated, keep strict enforcement disabled. The attempt20 patch only hardens fail-closed recovery and rollback uncertainty; it does not enable the feature.
