# Preserve an unhealthy ledger on task release

`unhealthy-release-fix.patch` follows `ac52c68`. Once the quota ledger is unhealthy or has not completed recovery, the release callback no longer appends a RELEASE or removes the active record. Already-running work can still finish; the unresolved allocation remains evidence and new governed GPU allocations stay fail-closed. This can require manual reconciliation after the job disappears, and does not provide automated recovery. Healthy reconciled releases retain the existing durable path.

No tests or live changes were made. Further checks must cover crash/short-write timing, missing completed jobs, and continued accounting before enabling the gate.

On 2026-10-30, `9491586` applied to the isolated u20 source and `nice -n 19 make -j2 -C src` exited 0 without warning/error lines in the incremental build log. Source-tree controller SHA-256: `3991a06b715a82010be47974dad43a4da96f1666643c1e19f2c61e44b981e92f`. The running services reported active; no candidate was installed or started.
