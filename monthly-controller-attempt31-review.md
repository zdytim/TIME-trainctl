# u20 controller attempt31/19 independent static review

No deployment or tests. Strict gate must remain hard disabled. Fix or explicitly mark these blockers:

1. TRES reconfigure invalidation clears old GPU TRES position and verified flag before reconcile, so the comparison to new position is skipped; preserve a trusted fingerprint across transitions and freeze if mapping or allocation evidence cannot be established. Do not trust unverified user data.
2. `_decision_locked` ignores `_checkpoint_locked` failure after persisting START_INTENT and returns admit success; commit also ignores checkpoint failure. Define crash recovery and whether WAL alone provides guarantee; if not proven, treat failures as unknown and fail closed. Avoid silent allow.
3. `_preempt_jobs` happens before quota admission; an ineligible job may alter others where preemption enabled. All effects must be behind an early safe precheck or unsupported modes blocked. Slurm19 has no proven single transaction; strict race between quota check and GRES allocation remains unresolved, void `monthly_quota_commit` cannot undo actual side effects on accounting failure.
4. Normal epilog still records UNKNOWN, all GPU work freezes, and unknown resolution is disabled; trusted node release evidence and auditable recovery not implemented. Do not declare feature complete merely because it fails closed.

Target u20 single-node Slurm19. Preserve real running jobs, use exact official source, no tests/simulations/deployment/services/credentials. Isolated compilation allowed. Sibling outputs, report all residual blockers. Supervisor commits and pushes changes before deployment.
