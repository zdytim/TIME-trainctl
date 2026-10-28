# Independent static review of u20 attempt36 / attempt22

Strict rule remains compile-time disabled; candidate is not deployed and not production-ready. Review of controller-attempt22-review/patches confirms:

1. `_mq_gres_alloc_result` records UNKNOWN after GRES failure and marks a->committed=true; cons_tres only propagates error when `guarded` is true, but guarded requires !committed. Recording UNKNOWN can inadvertently make the select ignore allocation failure. Separate operation phase from committed status; errors must propagate independent of accounting event.
2. Enabled CPU requests gpu=0 create no ledger record, but selected_allocation is called for all non-test jobs and requires record, so CPU jobs may be rejected. Disabled path still sets mq_intent_open for non-test jobs and calls abort; `_finish` does not check enabled and can alter clock/health on missing TRES. New GRES hooks also register/lock when disabled; disabled-mode equivalence is not demonstrated.
3. GRES hooks are added in libslurmfull, but build/installation packages slurmctld and select_cons_tres.so only, not matching libslurmfull; old shared library may lack symbols. Select plugin staging is not proof target PluginDir loads it. Audit dynamic symbols, versioned libraries and all Slurm daemons/rollback before any installation.
4. Normal GPU epilog marks UNKNOWN and global unhealthy; RESOLVE_UNKNOWN is disabled so next GPU jobs can remain frozen indefinitely. Need trusted, idempotent physical release evidence and recovery. Do not relax unverified release to improve liveness.
5. Allocation linearization, disk IO and production build equivalence remain unproven. Never claim strict condition met.

No runtime tests/deploy, source static audit only. This file is an instruction to repair/research, not approval to enable. Target u20 Slurm19; user requested no tests unless explicitly requested. Isolated compile and static check only.
