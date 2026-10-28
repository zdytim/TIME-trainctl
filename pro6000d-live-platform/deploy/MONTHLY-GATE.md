# Monthly GPU-hours: strict pre-allocation design

**User decision: STRICT. Once normal credit is exhausted, new and pending jobs
must not obtain a GPU allocation. The node-side SPANK execution-gate proposal is
REJECTED and SUPERSEDED. Do not build, install, or enable it.**

Its two prototype files have moved to `historical/`; the C source deliberately
contains a compile-time error and the plugstack line is commented out. They are
retained solely as design history and are not installed by the incremental
upgrade script. The source-backed implementation plan is
`STRICT-ALLOCATION-DESIGN.md`. The remaining execution-gate text below is historical
and MUST NOT be interpreted as current activation instructions.

## Historical rejected design

Status: source prototype only. Not installed, compiled, or exercised on either
host. Do not enable the example plugstack entry or claim strict PENDING gating.
The backend agent has explicitly deferred the ALLOW socket handler. Consequently
there is currently NO compatible permit server, and enabling this plugin would
reject all task launches. This is an activation blocker, not an optional feature.

## Boundary

The required SPANK remote `task_init_privileged` callback obtains job ID, step ID,
UID and restart count from `spank_get_item`, before user execution. It never reads
identity or a bypass flag from job environment/arguments. The same callback must
cover the batch task and every subsequent srun task. This protects execution, not
allocation: Slurm can allocate GPUs and display RUNNING before the callback. A
shell wrapper, even root-written, is not a substitute.

No Prolog/Epilog hooks, cancellation, requeue, priority edits or Slurm restarts are
performed by this plugin. A failed callback denies that launch. Actual lifecycle
behavior, including whether a failed new step affects its batch command and its
siblings, must be established for each deployed version before enforcement.

## Agreed backend protocol

Fixed Unix stream socket `/run/pro6000d-training/gate.sock`, root:root 0600.
Parent directory must be root-owned and not writable by group/others. The plugin
requires a root server via SO_PEERCRED; server must independently require a root
peer. The ten-second deadline covers the entire exchange.

Request (four unsigned decimal values, one newline):

```
P6G1 JOB_ID STEP_ID UID RESTART_COUNT
```

Successful response must echo the complete request prefixed with `ALLOW ` and
then CLOSE the connection. Anything else, missing EOF, extra bytes, missing
trusted items, insecure socket, or timeout denies execution. Server must NOT
allow on daemon startup merely because enforcement is disabled: leave the plugin
uninstalled while staged, or answer DENY for new permits.

Backend must resolve authoritative controller identity, check job owner/restart
against SPANK, and bind a permit to cluster identity, broker UUID/comment,
SubmitTime, allocation StartTime and restart generation. Job ID and restart alone
are insufficient because IDs are reused. Never trust client environment or permit
files writable by the job owner. Unknown identity or unsupported heterogenous /
array interpretation must deny new permits until explicitly implemented.

The first permit is committed durably in the same serialized decision that
settles all ongoing allocations for the UID, checks monthly credit and records
the admitted allocation. Concurrent tasks must see the same idempotent permit.
Later steps in that exact allocation reuse the durable permit even if credit is
negative or the calendar month changes. Requeue/restart creates a new allocation
and requires a new decision. Unknown controller identity must never fall back to
an old JobID-only permit.

Existing processes are never signalled. Backend outage currently prevents NEW
steps, including steps of an already admitted job: there is deliberately no
unsafe offline JobID cache. A training script may itself abort if its later srun
fails. Therefore uninterrupted continuation of all admitted workflows during
backend outage is an OPEN BLOCKER, not an implemented guarantee. An independent
durable permit service with verified allocation identity is needed for that
stronger guarantee. Controller outage is likewise fail-closed for new tasks.

## Activation prerequisites

1. User accepts execution-start semantics and possible short allocation before
   rejection, rather than strict PENDING-to-RUNNING prevention.
2. Check both queues immediately before migration. Do not trust an earlier empty
   snapshot. If not empty, stop migration; do not kill jobs. Adoption of already
   running allocations requires verified durable permits before any new steps.
3. Complete accounting recovery and log-retention capability first; configure
   month timezone and exact tier semantics. Accounting gaps block new permits.
4. Install the independently built binary root-owned and not group-writable by
   atomic rename. The .so must match the installed Slurm major version.
5. Install required entry only into the actual configured PlugStackConfig after
   inspecting its include topology. Preserve existing required plugins. Never
   overwrite the entire plugstack or change global user privileges.
6. Establish batch + srun callback coverage, denial without user execution,
   restart/ID-reuse handling, failure effects, accounting recovery and no
   pre-permit path via PAM adoption/containers/alternate execution channels on
   each installed version. This task has not run these checks.
7. Then enable monthly enforcement as a separate reviewed activation. Do not
   silently enable it during a code upgrade. No whole-node DRAIN or ordinary
   Prolog failure mechanism should be used for monthly credit exhaustion.

Build commands, for later authorized deployment (not executed here):

```
# pro6000d: installed custom Slurm headers
gcc -shared -fPIC -O2 -Wall -Wextra -I/opt/slurm/current/include \
    monthly_gate_spank.c -o monthly_gate_spank.so
# u20: requires matching Slurm 19.05.5 development headers FIRST
gcc -shared -fPIC -O2 -Wall -Wextra \
    monthly_gate_spank.c -o monthly_gate_spank.so
```

u20 currently lacks `/usr/include/slurm/spank.h` and `slurm.h` according to the
parent's read-only inspection. Do not copy a binary compiled for pro6000d or use
current headers to build for Slurm 19. Install matching development headers in a
separately authorized dependency step. This prototype does not install packages.

## Priority migration proposal (not applied)

Monthly balance and scheduling priority are separate controls. On pro6000d,
`PriorityWeightQOS=0` means changing QoS Priority alone has no scheduling effect.
Inspect PriorityType, all weights, partition tiers, QoS preemption/limits, and
existing associations before selecting a nonzero QoS weight. Use non-preempting
debug > important > normal priorities; do not change running job limits or apply
GrpTRESMins / full-time reservations as a substitute for the requested policy.
Queue fairness and resource-fit/backfill may still allow a lower tier to start.

u20 `priority/basic` plus `accounting_storage/none` cannot promise the same native
QoS ordering. Two explicit choices: broker-controlled ranking of platform-held
work (a different scheduling policy), or a separately planned accounting and
multifactor migration. Do not switch priority plugin, install slurmdbd or restart
Slurm as an incidental part of the monthly code upgrade. Execution gate deciding
a lower tier at launch does not retroactively change its place in the queue.

## Incremental staged code upgrade

`upgrade-platform-staged.py --admin ubuntu` (pro) or `--admin u20` (u20) is a
separate destination-side root script, not invoked by this prototype. Review
PlugStackConfig before use and do not use it where a required execution gate is
already installed; it refuses an enabled monthly config or an existing gate
socket. It backs up touched files, obtains an online SQLite backup, installs all
backend Python modules and frontend assets, preserves configuration with
`monthly_quota.enabled=false`, then restarts only the backend and reads state as
the actual admin UID. Failure restores touched code/config, restarts old backend,
and preserves the current database. Backend migrations must stay additive.

It does not activate SPANK, change priority, install packages, restart Slurm,
enroll users, or edit PAM. New backend helpers, notably `monthly_quota.py`, must be
present before it starts. It does not copy the whole database over a live WAL.
Existing extra frontend files are preserved. File replacement is atomic per
file, not a transactional switch of the entire frontend directory; a browser
refresh during the short upgrade can briefly see mixed assets. Backup location
and rollback health are recorded in the root-only deployment manifest.

After copying the reviewed complete source tree to the destination, change into
that tree's root (the directory containing `backend/`, `deploy/`, and
`cockpit-plugin/`). Invoke exactly the appropriate command on that host:

```
# pro6000d only
sudo /usr/bin/python3 ./deploy/upgrade-platform-staged.py --admin ubuntu
# u20 only
sudo /usr/bin/python3 ./deploy/upgrade-platform-staged.py --admin u20
```

Do not run either command on the Mac. The script's pre-upgrade state read accepts
the old API without monthly fields. After backend restart it retries transient
client/socket readiness for at most ten seconds, sleeping at most 0.5 seconds;
GPU or semantic API errors fail immediately. The upgraded response must include
`monthly_quota.enabled` equal to the JSON boolean `false`. Rollback health uses
the same bounded readiness window without requiring monthly fields.

## Evidence

- https://slurm.schedmd.com/spank.html
- https://raw.githubusercontent.com/SchedMD/slurm/slurm-19-05-5-1/slurm/spank.h
- https://raw.githubusercontent.com/SchedMD/slurm/slurm-19-05-5-1/src/common/plugstack.c
- https://raw.githubusercontent.com/SchedMD/slurm/slurm-19-05-5-1/src/slurmd/slurmstepd/mgr.c

The 19.05.5 implementation reads restart count from its internal step record.
This supports trusted request construction, not an end-to-end security claim.
