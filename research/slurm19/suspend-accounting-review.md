# Suspended GPU allocation accounting

The original Slurm 19.05.5 `src/plugins/select/cons_tres/job_test.c` describes action 2 as removing cores only on suspension; `select_p_job_suspend()` invokes that action. GRES ownership is retained. The recovered quota draft instead set the active GPU count to zero on indefinite suspension, so it would stop counting GPU occupancy while Slurm still holds those resources.

`handoff-accounting-fixes.patch` applies to the original recovered `monthly_quota.c` (SHA-256 `c5d46f7629d60d3e4aaed1ee373fcf8ca4e78db9d019076f13de33f303e5eb7a`). It includes the clock-check correction and changes the candidate to keep counting across suspend/resume. Recovery compares suspended jobs' GPU ownership as it does running jobs. Old draft WAL SUSPEND/RESUME records are rejected rather than silently replayed with uncertain billing. This is intentional fail-closed behavior; a migration would require separately audited accounting reconciliation.

The patch applies only to the isolated interrupted draft. It has not been built against u20 or dynamically verified. Slurm plugin variants, all recovery paths, precise GPU counts, concurrency and user priority policy remain to be audited. The monthly gate is not deployed or enabled; no tests, services, or jobs were run.
