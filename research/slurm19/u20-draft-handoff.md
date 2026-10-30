# Slurm 19 draft handoff

The interrupted u20 task left unreviewed source changes under its isolated `work/source-working` directory. A subset has been copied to the Mac's untracked `u20-handoff-work/slurm19/` for review, not deployment. The scheduler diff places a read-only quota check after `_get_req_features`, an intent before selector and GRES side effects, and a ledger commit after `allocate_nodes`. A failed node-info update marks the quota result ambiguous; its impact on partially changed GRES and node state is not yet established.

This is an incomplete snapshot, not a complete patch: the full changed-file inventory, all scheduler paths, job completion accounting, lock ordering, WAL durability, cgroup isolation and build compatibility still need review. GPU admission semantics are not proven and the gate must remain disabled. No tests, restarts, service changes or deployments occurred.
