# u20 Slurm 19 handoff: time check

In the interrupted draft `monthly_quota.c` SHA-256 `c5d46f7629d60d3e4aaed1ee373fcf8ca4e78db9d019076f13de33f303e5eb7a`, `_now()` returns zero on success and -1 on failure. `_check_shape_and_quota()` used `!_now(&now)`, so successful clock reads were reported as failures and eligible GPU jobs were rejected. `clock-check-fix.patch` corrects this inverted condition in a separate candidate copy.

This single fix does not establish correctness of allocation, priority tiers, accounting, recovery, scheduler integration or device isolation. The draft remains incomplete, disabled and uncompiled in the actual u20 environment. No tests, deployments, restarts or workload operations were performed.
