# Controller-owned quota read snapshot

`controller-read-snapshot.patch` follows `c97852d`. It adds an internal slurmctld-only read primitive returning one user's current Beijing month, precise projected GPU-seconds/nanoseconds used, grants, total capacity, remaining or overage, tier, active allocation/GPU counts, observation timestamp and durable WAL sequence under the ledger mutex. Failed, disabled, unhealthy, unreconciled, overflowing or ambiguous state leaves the caller's output untouched. The WAL position alone is not a cache key because projected running usage changes as time passes.

This is not an RPC or platform endpoint. A future transport must authenticate and authorize callers, expose only permitted users' data, and avoid treating a snapshot as an allocation permit. Allocation identities, authenticated grant acknowledgement, durable recovery and behavioral validation remain outstanding. No production components were changed and no tests were run.
