# Review 9 report

## Fixed or explicitly fail-closed

1. **Normal finalization wait.** `transition_state` now returns `RE_OK` for
   `FINALIZATION_REQUESTED` while retaining `RE_UNKNOWN` and usage. It does
   not create the node blocker, so a later positive cleanup event can release
   the exact generation. Negative, restart, ordering, authentication, and
   persistence faults remain durable barriers where possible. Administrative
   recovery is exact-generation only and cannot clear the node barrier.

2. **Missing or new journal.** `re_store_open` uses no `O_CREAT` and returns
   `RE_NOT_FOUND` for a missing path. `re_store_initialize` is explicit,
   exclusive, mode/owner checked, parent-`fsync`ed, and records a nonempty
   externally verified anchor. Empty, anchor-mismatched, or unverified logs
   remain `RE_UNINITIALIZED`; the external anchor verifier must reject
   recreation for an existing node.

3. **Append/fsync ambiguity.** Events use `PREPARE` and `COMMIT` records,
   each fsynced at its phase, with state publication only after the commit
   witness and commit marker. A prepare without commit, malformed tail, or
   replay ambiguity fails closed. A post-write fsync error can still be
   inherently ambiguous to a local filesystem; the design records that limit
   and remains disabled pending the real controller durability contract.

4. **Administrative recovery identity.** Recovery still requires the stored
   allocation generation, node, old boot ID, authorized peer, cgroup identity,
   exact device lease, and positive cleanup evidence. `verify_admin` only
   authenticates the authorized action and cannot substitute for cleanup proof
   or clear a node barrier.

5. **Idempotency capacity.** The 256-entry index is durable and spans
   generations. Old retries and conflicts remain detectable while capacity is
   available. No unsafe eviction is implemented; exhaustion freezes the store.
   Production needs durable tombstones or an externally anchored compaction
   protocol, which is absent and therefore a release blocker.

6. **START and negative/partition data.** All external event kinds require
   bounded NUL-terminated cgroup data. START rejects negative evidence,
   controller-partition, admin approval, missing cgroup identity, and missing
   device token before RUNNING can be created. Cleanup positive evidence also
   rejects negative and partition flags.

7. **Admission versus observation.** `re_store_admission_gate` checks the
   persisted node barrier under the store mutex. `observe` masks a released
   allocation to UNKNOWN and usage-held if its node is blocked. This is a
   library predicate, not atomic Slurm scheduling integration; the production
   transaction is still missing.

8. **Field validation.** All event cgroup fields are checked for a NUL within
   their fixed bound before use. Replay validates fault and commit records.
   Equality uses `field_equal`, which repeats bounded termination checks before
   `strcmp`. Malformed records fail replay closed.

9. **Node restart scope.** A restart persists a node/boot tracker with a
   node-wide blocker. It causes all observations for that node's previously
   released states to return UNKNOWN, freezes future starts, and keeps old
   usage held. The event's single allocation state is not used as the only
   barrier.

## Source and verification

The new sibling contains only the header, source, design, local capability
record, Makefile, report, and manifest. The prototype remains disabled by
default and has no Slurm integration, authenticated transport, verifier,
physical GPU cleanup proof, scheduler atomicity, or production persistence
adaptation.

Only compile-only syntax checks were authorized and run for both
`RELEASE_EVIDENCE_ENABLED=0` and `=1`, using C11, warnings as errors,
pedantic mode, and pthread declarations. No tests, simulation, installation,
service restart, active job, credential access, deployment, or commit was
performed.
