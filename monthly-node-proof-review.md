# Independent static review of node cleanup prototype

Source controller-node-proof-result/j-81ad4dc3ca50405f9d5c/4/outputs/release-evidence-prototype. Current code disabled and not integrated; keep disabled. Repair or document:
1. open uses O_CREAT, follows symlinks, no regular-file ownership/mode verification, exclusive lock or parent directory fsync; establish secure state dir and durable journal ownership.
2. apply_state publishes RELEASED before append/fsync; observe may see release before durable event. Use mutex and durable-before-visible semantics; single process caller assumptions insufficient for multi-thread/process.
3. sequence tracked per generation, not per node+boot; event id dedup only most recent, misses old retries and cross-generation identity. Persist durable global sequence and bounded replay/idempotency index with conflict detection.
4. Valid authenticated negative evidence/restart produces RE_UNKNOWN_RESULT negative yet replay treats all negative as corruption; normal UNKNOWN must replay as fail-closed, distinguish corruption. Authentication/sequence/conflicting duplicates force unknown in memory only and restart loses it; persist fail-closed safely.
5. RE_EVENT_NEGATIVE_EVIDENCE does not itself set negative, NODE_RESTART new boot rejected prior to durable transition; enforce exact state transitions.
6. re_store_open allows apply before replay; require successful recovery before new event/state publication. Need robust journal first creation, parent directory fsync, verify files.
7. No Slurm integration, actual authenticated transport, verifier or GPU cleanup evidence exists. Do not claim trustworthy proof or enable. Unknown, partition, node reboot and missing evidence freeze new allocations while existing jobs continue. Only genuine privileged node identity + verified cgroup/GPU evidence can establish safe release.

No tests, no production changes, no deployment. Isolated static compile only. Keep outputs small with no symlinks, write artifacts into dispatcher-approved sibling outputs; supervisor reviews, commits and pushes before any deployment.
