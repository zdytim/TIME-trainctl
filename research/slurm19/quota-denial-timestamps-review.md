# Staged final-admission denial cleanup

In the Slurm 19 candidate, immediately before the final `monthly_quota_begin` check, the scheduler sets tentative start, last-active and end times. If the quota check then rejects admission, the generic cleanup frees the node bitmap but did not clear these timestamps. This can leave a pending, unallocated job with misleading time fields after exhaustion, ledger failure or another final-admission denial.

The staged patch clears those three tentative fields and marks the job updated on this rejection path, consistent with adjacent pre-launch failure paths. It does not change active jobs, perform a GPU allocation, or resolve the separate scheduling-order race. Only source inspection was performed; pending-state, queue, accounting and crash behavior require isolated validation. No service or production job was modified.

The patch from commit `3071c20` was applied to the existing isolated candidate on u20. `make -j2 -C src/slurmctld` recompiled `node_scheduler.c` and linked successfully; the resulting ELF SHA-256 was `503eedc39c162f7eadd717c13e6d04cae87428a780defbf331c032ec03dcb208`. The existing Slurm and training services reported active after this incremental build. The candidate was not installed or executed; no test or workload was run.
