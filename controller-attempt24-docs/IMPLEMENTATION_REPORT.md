# u20 Slurm 19.05.5 monthly quota controller — attempt24

Attempt24 is a reviewable, compile-checked source revision against the verified official
slurm-19-05-5-1 source at commit
78e9cfab783aeed0da68ea25532a3f35114d4072. It remains a disabled prototype:
MQ_STRICT_INTEGRATION_PROVEN is hard-coded to 0, and explicit enablement still rejects
because Slurm19 allocation linearization and bounded durable scheduler I/O are unproven.

The revision repairs the reviewed defects as follows.

- Reconfiguration retains a trusted GPU TRES fingerprint in memory and in a TRES_MAP
  journal event. The identity includes TRES id, array position, type, and name. A changed
  mapping leaves the previous identity intact, marks the authority ambiguous, and blocks
  new GPU work. A missing or unverified map is never interpreted as CPU zero.
- START_INTENT admission requires the checkpoint result. If checkpoint publication fails,
  the operation is poisoned and an UNKNOWN record is attempted before returning deny.
  monthly_quota_commit() treats clock, WAL, checkpoint, missing-intent, and zero/unknown
  actual-GPU failures as errors; after a GRES success, accounting failure marks the
  operation UNKNOWN and unhealthy rather than silently allowing it.
- _preempt_jobs is preceded by an immediate monthly safety recheck. Enabled operation
  rejects unsupported preemption/recovery states before that side effect. The adapter still
  requires PreemptMode=off and PreemptType=preempt/none at initialization.
- Final epilog completion remains conservative UNKNOWN. Slurm19's normal epilog and
  make_node_idle paths do not prove process/device cleanup, so no release or administrative
  resolution was invented. This can freeze later GPU work and is an explicit liveness
  blocker.

The supported prototype scope remains single-node ordinary gpu:1 jobs, no arrays,
heterogeneous/packed jobs, burst buffers, typed GRES, resource resize, or preemption. CPU
jobs and disabled mode retain the prior no-op behavior. Matching slurmctld,
libslurmfull.so, and select_cons_tres.so artifacts are packaged together with relative
checksums bound to the reviewed patch.

Static and isolated build evidence is in evidence/final-static-review.txt,
evidence/build24c.log, evidence/config.log, and evidence/dynamic-symbol-audit.txt.
No tests, scheduler simulations, service operations, installation, deployment, commit, or
push were performed.

Remaining blockers are material. Slurm19 has no proven atomic transaction across scheduler,
select/cons_tres, GRES, burst-buffer, and the monthly WAL; the quota check can still race
physical allocation, and accounting cannot undo a partial GRES/select side effect. Durable
fsync/checkpoint work while scheduler locks are held has no established bounded latency.
Normal completion still lacks trusted physical cleanup evidence and audited idempotent
UNKNOWN recovery. Production PAM/munge, configured GPU AccountingStorageTRES, plugin loader
paths, ABI match, live reconfigure behavior, and installed runtime state require supervisor
validation. This artifact is not production-ready and must remain disabled.
