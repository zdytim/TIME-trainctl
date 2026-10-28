# Slurm 19.05.5 allocation and completion audit

The exact clean baseline is tag `slurm-19-05-5-1`, commit `78e9cfab783aeed0da68ea25532a3f35114d4072`. The source was inspected locally; no production controller, scheduler state, credentials, or host configuration was read.

## Allocation order

The clean upstream scheduler establishes the following order in `src/slurmctld/node_scheduler.c`:

- `:2735-2746` runs burst-buffer stage-in readiness before a placement is accepted.
- `:2862-2883` calls `_preempt_jobs` for a non-preview scheduling pass. This can signal, kill, checkpoint, or requeue preemptees before the monthly gate at `:2960-2975`.
- The attempt20 adapter performs preview/admission at `:2960-2975`, before the following side effects.
- `:3002-3034` calls `bb_g_job_begin`; `src/slurmctld/burst_buffer.c:621-638` invokes every burst-buffer context while holding `g_context_lock`.
- `:3035-3049` calls `select_g_job_begin` and, on failure, calls `bb_g_job_revoke_alloc`.
- `src/slurmctld/burst_buffer.c:641-665` documents that revoke does not release resources allocated previously.
- `:3085-3103` calls `select_g_select_nodeinfo_set`. For `select/cons_tres`, `src/plugins/select/cons_tres/select_cons_tres.c:189-237` implements `_add_job_to_res`; its `:223-236` calls `gres_plugin_job_alloc` and updates select state.
- `:3105` calls `allocate_nodes`, which changes node state in `src/slurmctld/node_scheduler.c:396-444`.
- `:3120-3124` calls `job_set_alloc_tres` and then the adapter commit with `tres_alloc_cnt`, the first point in this path at which the selected actual TRES count is materialized for the patch.

The ledger mutex serializes ledger state, but it is not held by the select/GRES plugin transaction and does not prevent another scheduler path or plugin callback from consuming or changing resources. Therefore admission before selection races with physical allocation, while commit after selection cannot undo side effects. The new attempt20 error hook records UNKNOWN for a select-nodeinfo failure; it does not claim rollback.

## Completion order

`src/slurmctld/node_scheduler.c:500-503` invokes `select_g_job_fini` before constructing and queuing the terminate RPC. `:623-627` can call `cleanup_completing` immediately when no node completion response can be awaited. `src/slurmctld/job_mgr.c:15143-15243` handles the later epilog callback and calls `make_node_idle`; `src/slurmctld/node_mgr.c:3935-3972` removes the node from the job bitmap and may call `cleanup_completing`.

Neither `select_g_job_fini` nor dispatch of a terminate RPC is trusted proof that processes and devices are gone. Slurm 19 has no adapter-visible proof tying GRES release, container cleanup, and the final epilog callback into one durable transaction. Attempt20 therefore retains an allocation as durable UNKNOWN at the final epilog boundary (`job_mgr.c:15249-15255` in the patched source). It also retains UNKNOWN on down-node and re-kill paths. This intentionally freezes later enforcement rather than undercharging.

`batch_requeue_fini` starts at `job_mgr.c:15257` and clears the old timing and node identity. The final-epilog hook is placed before it, preserving the old accounting identity. The hook is idempotent because an existing UNKNOWN allocation is not appended twice.

## Reconfigure and recovery

`src/plugins/select/cons_tres/select_cons_tres.c:2226-2284` rebuilds select state on reconfigure. It re-adds running and suspended jobs and has a special cleaning-job release path after 300 seconds (`:2260-2279`). That path is not proof of device cleanup. The adapter invalidates the cached GPU TRES map before reconfigure recovery and refuses new GPU decisions until the map, journal, cutover, restored jobs, and API barrier are reconciled.

A missing restored job, missing/ambiguous GPU count, journal/fence failure, or inconsistent recorded allocation remains unhealthy/ambiguous. The persisted migration cutover clips only the unverified pre-cutover interval; it does not erase a real allocation.

## Result

The source exposes no safe hook that proves a physical allocation commit and durable quota commit are one linearizable operation. Strict integration remains disabled by `MQ_STRICT_INTEGRATION_PROVEN 0`; explicit enablement rejects initialization. This is a source audit and isolated compile result, not production readiness.
