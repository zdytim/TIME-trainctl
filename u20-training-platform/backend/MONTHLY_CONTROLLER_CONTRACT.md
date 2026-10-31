# Monthly allocation gate contract — historical design

> This document records the original design and its status at the time it was
> written. Statements below that the gate is unimplemented or disabled are
> historical, not current deployment status. The u20 Slurm 19 controller was
> subsequently deployed and its monthly gate activated; see
> [u20 cutover](../../deployment-records/2026-10-31-u20-monthly-staged-cutover.md)
> and [u20 activation](../../deployment-records/2026-10-31-u20-monthly-ui-activation.md).
> Those records describe the tested scope and remaining limitations. They do
> not establish deployment or enforcement on pro6000d or any other host.

Required outcome: an exhausted user's unstarted jobs remain PENDING and receive no GPU allocation. Already allocated jobs continue and accrue overage. Node-side SPANK rejection is not an implementation of this requirement. Neither the current Python API nor sacct polling is authoritative at the allocation decision instant.

## Authority and serialization

The scheduler controller must own the admission state and actual allocation lifecycle. The platform API owns authentication, applications and the user interface; it must not issue a cached ALLOW token. SlurmDBD/sacct provides historical reconciliation, not instantaneous permission.

For each scheduling decision, under the same controller synchronization domain that commits GPU allocation:

1. Resolve cluster, UID, stable job identity (submit/DB identity plus allocation/restart generation) from controller records. Never trust browser fields, job environment, PID or job ID alone.
2. Settle every open allocation interval for that user through the decision instant. Sum concurrent allocations; actual allocated GPU cardinality is the multiplier. Split intervals at Asia/Shanghai month boundaries. GPU ownership is charged until release, including a suspended process if its allocation retains GPUs. Requeue ends an allocation generation; its next allocation must pass admission again.
3. Apply durable grant commands in sequence, once per application ID and bound month. A grant changes normal capacity; it first offsets already accrued overage. Exact zero balance is ineligible.
4. Compute eligibility and tier from the same revision. If evidence/recovery state is incomplete or total balance is exhausted, leave PENDING with a distinguishable monthly-budget reason. Do not call allocation commit or node launch. Continue considering other jobs.
5. If eligible, commit the allocation and corresponding billing interval as one recoverable controller operation. Persist an intent/commit journal and reconcile it against recovered controller allocation state after crashes. No successful allocation may escape accounting; no rolled-back allocation may be billed as running. Storage failure denies new allocations, not existing execution.

All main scheduling, immediate scheduling, backfill RUN_NOW, resource reservation prediction, requeue and supported heterogeneous/array paths require review. Rejecting only one path is insufficient. A speculative backfill/WILL_RUN evaluation must not create charges or a reusable admission permit. An eligibility check performed before releasing locks is insufficient if allocation happens after another thread changes state; recheck at commit.

Persist integer GPU seconds and a subsecond remainder (or an equivalently exact accumulator) so frequent updates cannot discard fractional use. Persist clock anchors; controller restart, clock discontinuity or incomplete event recovery requires reconciliation before allowing new allocations. Do not reconstruct a completed, suspended or resized interval by extrapolating the latest polling timestamp.

## Priority

Tier is a per-user, per-device, per-month function of the same settled total: debug, then important, then normal. All that user's pending jobs must be reprioritized on a tier boundary, grant, month rollover or usage recovery. At candidate evaluation, recompute tier and invalidate a stale queue ordering before commit. A single job can consume several tiers without being restarted; changing its accounting tier must not preempt it. Existing job duration limits are independent from tier.

The integration must update actual scheduler priority. A UI badge or a cached QoS string is insufficient. Audit interactions with priority weights, reservation/partition tier, nice and backfill rather than promise FIFO. For 26.05.5 and 19.05.5, inspect the exact source and supported hooks independently; no verified drop-in allocation hook has been delivered here. A maintained controller/select integration or upstream-compatible policy change is required, not a Python socket callback under controller locks.

## API boundary

Future controller commands: Grant(application_id, uid, month, gpu_seconds, actor, reason) and ReadSnapshot(cluster, revision). Grants are acknowledged only after durable controller application; retries return the same result. Snapshot includes settled usage, open allocation identities, accounted-through instant, tier, balance, overage, accounting health and controller revision. The UI ledger is a projection of this authority, not a competing writer. Current monthly_additions/usage tables are a staged implementation and need a migration/reconciliation plan before changing authority.

No ALLOW execution socket is implemented. Current enforcement_ready remains false; monthly_quota.enabled defaults false. Manually enabling the current ledger freezes new platform dispatch, but cannot prevent existing Slurm PENDING jobs from starting. Do not enable it as strict policy.

## Work still required

- Review both installed Slurm versions' complete allocation and recovery call graphs; choose a supported integration or maintained patch and document upgrade/rollback costs.
- Implement controller event journal, exact concurrent accounting, atomic pre-allocation eligibility and pending reasons.
- Implement actual dynamic priority plus durable acknowledged grant commands.
- Establish reliable historical recovery on u20; reconcile pro6000d history and explicit cutover without backbilling earlier use.
- Validate required scenarios before activation under separately authorized checks. No tests or scheduler mutations were performed for this design.

Official references: https://slurm.schedmd.com/select_design.html ; https://slurm.schedmd.com/sched_config.html ; https://slurm.schedmd.com/priority_multifactor.html ; https://slurm.schedmd.com/resource_limits.html . GrpTRESMins and safe do not implement the requested last-running-job overage semantics.
