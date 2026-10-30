# Proposed u20 isolated validation — not executed

## Boundary

Use a fresh, explicitly named amd64 container on u20 with its own process, network, mount, identity and IPC namespaces. No host networking, GPU devices, Docker socket, host Slurm config, Munge key, live state/database, user home or service control sockets may be mounted. Do not use `--privileged`, host PID/IPC namespaces or access real `/dev/nvidia*`. Limit CPU, memory, process count and writable space; use a container-only writable state directory and an ephemeral Munge key. Clean up only artifacts created for this validation. Obtain the image separately and verify its digest and compatible runtime libraries before use. Existing containers, jobs and services stay untouched. Disk was approximately 29 GiB free at the last read-only check; check again before preparing a bounded image/build.

An isolated fake-GRES Slurm setup must prove that it is using its own controller, slurmd, ports, user identities, ledger and scratch state before any workload is submitted. If the container cannot model fake GRES without host device mounts or needs privileged host access, stop rather than weaken isolation. Synthetic clock controls must affect only the isolated candidate; record the mechanism and limitations. Never use copied production ledgers as fixtures.

## Required observations

1. Check default disabled behavior and startup with missing, empty, truncated, mismatched or malformed WAL/checkpoint: the feature must not silently reset usage or admit governed GPU jobs.
2. Seed controlled monthly usage and check debug 5, important 100, normal 500 GPU-hours; verify each transition and exact-zero exhaustion. Four allocated GPUs accrue four GPU-hours per elapsed hour. Check subsecond accounting, concurrent allocations and Beijing month boundaries.
3. With normal exhausted, verify an unstarted job remains pending with no GPU allocated, including main, backfill and direct Slurm submission paths. Already allocated jobs continue through completion and accrue overage. Grant additions first offset overage; retries with the same application ID do not apply twice and changed fields conflict.
4. Cover interrupted PREPARE, ambiguous selection, COMMIT, release, WAL/checkpoint fsync and restart. Unknown state must fail closed for new GPU allocations and preserve existing work. Include replay generation reuse, time regressions, and a WAL-only complete-prefix rollback. Do not claim detection of rollback of both WAL and checkpoint together.
5. Check API/MCP identity, owner filtering, admin-only approval, token replay, lost controller/broker replies, outbox reconciliation, exact payload binding, terminal consent cancellation/expiry and no-terminal read-only behavior. Verify non-admin attempts are denied.
6. Independently inspect queue ordering at tier transitions, partition and reservation precedence, FIFO and backfill. A cached snapshot must not be mistaken for a fresh allocation authorization.

A fake-resource container cannot establish real GPU DAC/cgroup isolation, NVIDIA/PyTorch behavior, live-service ABI safety, or production failover. Those require a separate explicit authorization and a protected maintenance plan. Document precise outputs and limitations, not just pass/fail. Any failed or unobservable invariant blocks activation.

## Authorization and preparation

The project's AGENTS.md currently prohibits running tests without an explicit user request. This charter is a proposed validation, not authorization. No container, test workload, simulator, training job or live service mutation was started in preparing it. Image inventory showed an amd64 image is present, but image contents and suitability for this setup have not been verified. No image has been pulled or changed.
