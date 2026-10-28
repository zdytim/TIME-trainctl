# Strict monthly GPU-hours: controller allocation design

Status: researched design, NOT an implemented Slurm patch. No daemon/config
changes, builds, or execution tests were performed. User selected strict
PENDING/no-allocation semantics; the former SPANK proposal is superseded.

## Contract

Monthly bands are debug 5 GPU-hours, important 100, normal 500, with actual
allocated GPU count times occupied duration. Preserve the approved waterfall
policy; do not reserve the requested maximum job duration. Exhausted normal
credit blocks every NEW allocation for that UID, including already pending work.
Already allocated jobs keep running through negative balance. Additional credit
offsets debt before creating positive balance. No cancellation, node drain,
preemption, or shortened walltime is introduced by this policy.

The allocation decision is a linearizable controller transaction. Existing
allocations are settled to that transaction's timestamp before admitting another
allocation. Admission is never based on a periodic API cache, release-time token,
or a balance checked before waiting for the controller's locks.

## Verified common allocation paths

Exact upstream tags researched: `slurm-19-05-5-1` and `slurm-26-05-5-1`.

| Source | 19.05.5 | 26.05.5 |
| --- | --- | --- |
| Main scheduler `job_scheduler.c` calls `select_nodes` | near 1669 | near 1798 |
| Backfill `_start_job` calls `select_nodes` | near 2731 | near 3911 |
| Immediate submission `job_mgr.c` reaches same function | `_select_nodes_parts`, near 4776 | `_select_nodes_qos`, near 3949 |
| Resource-selection completion `node_scheduler.c` | `select_g_job_begin` near 2859, RUNNING 2901, nodeinfo 2903, allocate_nodes 2922 | corresponding 2795, 2837, 2839, 2858 |
| `select/cons_tres` job_begin | no-op, near 1277 | no-op, near 362 |

Official sources (line numbers are guides; use function names for patching):

- [19 node scheduler](https://github.com/SchedMD/slurm/blob/slurm-19-05-5-1/src/slurmctld/node_scheduler.c)
- [26 node scheduler](https://github.com/SchedMD/slurm/blob/slurm-26-05-5-1/src/slurmctld/node_scheduler.c)
- [19 main scheduler](https://github.com/SchedMD/slurm/blob/slurm-19-05-5-1/src/slurmctld/job_scheduler.c)
- [26 main scheduler](https://github.com/SchedMD/slurm/blob/slurm-26-05-5-1/src/slurmctld/job_scheduler.c)
- [19 backfill](https://github.com/SchedMD/slurm/blob/slurm-19-05-5-1/src/plugins/sched/backfill/backfill.c)
- [26 backfill](https://github.com/SchedMD/slurm/blob/slurm-26-05-5-1/src/plugins/sched/backfill/backfill.c)
- [19 immediate submission](https://github.com/SchedMD/slurm/blob/slurm-19-05-5-1/src/slurmctld/job_mgr.c)
- [26 immediate submission](https://github.com/SchedMD/slurm/blob/slurm-26-05-5-1/src/slurmctld/job_mgr.c)
- [19 cons_tres](https://github.com/SchedMD/slurm/blob/slurm-19-05-5-1/src/plugins/select/cons_tres/select_cons_tres.c)
- [26 cons_tres](https://github.com/SchedMD/slurm/blob/slurm-26-05-5-1/src/plugins/select/cons_tres/select_cons_tres.c)

The source exposes a practical shared enforcement point. It does NOT yet prove
coverage of every allocation/resize/resume/heterogeneous path; that call graph
audit is a required implementation work item, not an assertion of this document.

## Recommended implementation: small controller patch, shared ledger core

Retain the installed `select/cons_tres` resource allocator and ordinary Slurm
accounting. Build two version-specific controller adapters around one monthly
ledger core. Do not replace SelectType with an unverified forwarding wrapper:
that must preserve many private ABI/state/recovery operations, not just one hook.

1. Add a cheap, read-only monthly eligibility check early in `select_nodes` so
   exhausted jobs do not trigger expensive selection or preemption side effects.
   The deployed initial scope is single-node ordinary platform batch jobs with
   preemption disabled. Preserve existing jobs; reject unsupported new forms.
2. Add the authoritative transaction in the common real-allocation path after
   candidate selection but before any actual resource commitment, RUNNING state
   transition or launch. Pair it with the success/failure exits. A fresh timestamp
   is required: the function's earlier `now` is not the budget decision time.
   Inspect and document resource side effects in `_pick_best_nodes`, RUN_NOW
   select tests and their cleanup before fixing the final insertion point.
3. Under the controller's job/node write locks and a fixed ledger lock order,
   accrue all open allocation intervals for that UID, cross month/tier boundaries,
   determine effective tier, and synchronously journal a unique start intent.
   Unknown recovery state or nonpositive normal credit returns a nonfatal pending
   policy result. Do not convert it into a rejected batch job or admin hold that
   requires manual release after a top-up. Preserve an explanatory state reason.
4. The successful allocation path records authoritative allocated GRES, start
   timestamp, identity and a commit marker before task launch. Any failed path
   records ABORT and cleans up candidate resources; never bill abandoned intents
   as if they ran. The transaction boundary must encompass the actual allocation
   commit, not merely grant a reusable token before it.
5. Lifecycle hooks close/update intervals at actual resource release, resize and
   requeue. Controller completion versus final node release must be distinguished:
   COMPLETING can still occupy GPUs. Old completed jobs disappearing from memory
   must not erase their ledger history.
6. Main queue and backfill eligibility/ranking use the same effective tier.
   At final allocation, re-evaluate it; if a stale ranking used a higher tier,
   defer that candidate for rebuilding the queue instead of starting it with a
   stale priority. Existing running jobs are not requeued or reprioritized.

Do not perform `scontrol`, `sacct`, HTTP, or backend RPC from inside controller
write locks: these can deadlock or make scheduling depend on a GUI service.
Monthly policy/credit operations are versioned, idempotent controller ledger
transactions. The web backend requests changes and reads committed results; it
does not own an independent authoritative balance. The implementation must bound
journal latency and fail closed for new allocations when persistence fails.

## Recovery and missing accounting evidence

Keep a controller-local append-only journal plus checkpoints with checksums,
monotonic sequence numbers and fsync. Record policy changes, grants, START_INTENT,
START_COMMIT, allocation changes and resource RELEASE. Identity includes cluster,
job submit identity/platform UUID, allocation generation and restart count.

At startup, close the NEW-allocation gate first. Reconcile checkpoint/journal with
restored controller jobs and registered node steps before scheduling resumes.
An intent may have crossed a crash boundary into a real allocation: do not simply
delete it or trust that missing controller state means no running process.
Recovered active allocations are restored without reapplying new-job credit
checks. Their continuing use still accrues; requeue creates a fresh generation
that must pass admission. Never kill a recovered allocation because it exceeded
credit during downtime. Ambiguity blocks that user's new allocations until an
authoritative repair, without making up a zero-cost completion time.

pro's slurmdbd is useful for reconciliation. u20 needs durable allocation/release
evidence since accounting_storage/none cannot reconstruct missing intervals from
sacct. Merely polling jobcomp tails or relying on MinJobAge is insufficient. A
node-side durable release record may be needed to recover jobs that finish while
the controller is down; retain original timestamps, actual GPUs and generations.

Monthly reset uses an explicit fixed timezone and interval splitting; no midnight
cron race. Top-ups are serialized with allocation decisions. Historical debt is
not silently erased or moved into a different month. Clock regressions, unknown
release times and journal corruption disable new allocations rather than creating
credit. Resume/resize/recovery/array/heterogeneous job handling must be implemented
or explicitly refused for NEW work before claiming full coverage.

## Why stock accounting settings are insufficient

`GrpTRESMins` without safe mode can terminate jobs when usage reaches the limit.
Safe mode protects already running jobs but admits based on enough credit for
completion, changing the requested positive-balance/actual-use policy. Neither
is the required combination. [Official resource-limit semantics](https://slurm.schedmd.com/resource_limits.html)

`NoDecay` prevents QoS usage from decaying/resetting automatically; it does not
add a keep-running/deny-only-start mode. `DenyOnLimit` changes certain submission
rejections; it is not a start-only monthly budget policy. Shared QoS group usage
also must not be confused with each user's independent monthly allotment. Manual
reset scripts cannot provide atomic month boundaries and debt/top-up handling.
[Official QoS flags](https://slurm.schedmd.com/sacctmgr.html#OPT_NoDecay)

Keep ordinary accounting, association authorization and existing concurrency
limits, but leave monthly GPU-hour enforcement to the custom controller ledger.
Do not introduce native monthly limits that would independently kill running jobs.
The existing pro `PriorityWeightQOS=0` and u20 `priority/basic` must be accounted
for in the ranking adapter; merely writing QoS Priority is not a migration.

## Concrete remaining work and deployment boundary

- Obtain each installed build's exact source/configure options and compare with
  the researched tags; inspect the actual SelectType and scheduler settings.
- Implement the ledger core and two source adapters, with an audited call graph
  covering commit, abort, release, recovery and alternate allocation paths.
- Add authenticated administrator credit/policy commands and backend integration;
  replace stale balance-based release authorization with controller decisions.
- Add durable state/recovery ownership and package manifests. Build separate
  versioned binaries; never overwrite a running executable/plugin inode.
- Design activation with no new dispatch during cutover and preserved controller
  state. Controller code cannot be activated by a plain reconfigure; a planned
  controller binary replacement/restart is required. It must not restart slurmd
  or cancel active jobs. Rollback must retain new accounting/grant history.
- No behavioral checks were authorized or performed here. Source design alone
  cannot establish production safety; any later validation/activation is a
  separate authorized stage. Until then monthly enforcement remains disabled
  and the UI must not claim the strict rule is effective.
