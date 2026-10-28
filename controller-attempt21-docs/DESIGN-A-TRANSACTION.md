# Design A — coordinated select/GRES transaction

## Goal

Patch the exact Slurm19 path so the monthly decision linearizes at the same commit that makes a GPU unavailable to other jobs. This is the only design that could preserve normal scheduling throughput without relying on an unbounded scheduler lock. It requires a Slurm core/API change, not only an adapter.

## Proposed sequence for one node

1. Enter a controller-wide GPU allocation transaction before `_preempt_jobs` (`node_scheduler.c:2862-2883`) and before `bb_g_job_begin` (`:3002-3034`). The transaction owns a generation counter and a resource allocation gate. Every main scheduler and backfill caller must use it.
2. Perform pure selection into private copies. Current `select_g_job_test` is destructive of `avail_bitmap` (`node_scheduler.c:2158-2160`), and `select/cons_tres` builds mutable `job_resources` and GRES selections. The prepare operation must not mutate shared `node_usage`, node GRES, job GRES, or burst-buffer state.
3. Extend the common select ABI (`src/common/node_select.c:66-90`) with prepare, commit, rollback, and recovery callbacks. `select/cons_tres` must stage the `select_node_usage` and `part_res_record` changes instead of calling `_add_job_to_res` directly.
4. Extend the GRES interface. Today `_add_job_to_res` calls `gres_plugin_job_alloc` (`select_cons_tres.c:223-236`). That function iterates contexts while holding `gres_context_lock` and can partially mutate state before returning an error (`src/common/gres.c:10364-10424`). The new API must stage the exact device bitmap/count and atomically publish it, or provide an inverse rollback for every context and every partial failure.
5. Require burst-buffer contexts to expose prepare/commit/rollback. The current `bb_g_job_revoke_alloc` explicitly does not release previously allocated resources (`src/slurmctld/burst_buffer.c:641-665`), so it cannot be the rollback half of this transaction.
6. Persist a durable reservation before any physical mutation. The reservation contains job identity, source generation, UID, requested and expected actual GPU count, Beijing month, realtime/monotonic fence, and an operation ID. The ledger holds the reservation against the current running GPU charge.
7. Commit staged select/GRES/node state and obtain the actual count. `job_set_alloc_tres` currently materializes allocated TRES only at `node_scheduler.c:3120-3124`, after `allocate_nodes` at `:3105`. The new transaction must expose this count before publishing the resource state to competing scheduling paths.
8. Append and fsync `START_COMMIT` with the actual count, then publish the resource generation. If any step after reservation fails, execute every plugin rollback. If rollback is not proven complete, append durable `UNKNOWN` and globally reject new GPU allocations.
9. On final cleanup, require a trusted device/cgroup cleanup callback plus final-node epilog completion. `select_g_job_fini` at `node_scheduler.c:500-503` is only termination start. `job_epilog_complete` calls `make_node_idle` at `job_mgr.c:15143-15243`, while slurmd can notify completion before `container_g_delete` (`src/slurmd/slurmd/req.c:5482-5508`). Without a stronger callback, retain UNKNOWN.

## Exact race windows

- **Before the transaction gate:** preemption can kill/requeue jobs before the quota decision.
- **Between select and GRES:** selection can choose a bitmap while another path changes GRES state.
- **Inside GRES allocation:** multiple GRES contexts can partially mutate state before an error.
- **After node allocation:** `allocate_nodes` changes node state before actual TRES commit.
- **Commit crash:** physical state can be published with only `START_INTENT`; recovery must map the token to a restored job and actual device evidence.
- **Rollback failure:** current burst-buffer revoke and select cleanup do not prove inverse effects.
- **Release:** termination RPC, epilog completion, container cleanup, and GRES bookkeeping are separate events.

## Proof obligations

The design is acceptable only if a code review and behavior validation prove: every allocator path is gated; no callback has untracked side effects; exact GRES counts are known before publication; all failures are reversible or become durable UNKNOWN; lock ordering cannot deadlock; WAL and checkpoint latency has an operational bound or allocation backpressure; recovery is ordered after TRES/assoc initialization; realtime regressions and monotonic discontinuities freeze decisions; and existing running jobs are reconstructed with verified actual GPU counts. The current Slurm19 ABI proves none of these.
