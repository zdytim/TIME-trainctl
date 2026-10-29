# Review 10 report

## Findings and source mapping

1. **Initialization rollback.** The public chain fields and callbacks are in
   include/release_evidence.h:54-128. Initialization persists the expected
   chain head in append_initialization_record
   (src/release_evidence.c:466-487). apply_event_locked
   (src/release_evidence.c:1077-1202) enforces previous-head continuity,
   durable-record count progression, and external verify_commit before COMMIT
   or a chained fault record. replay_locked
   (src/release_evidence.c:1217-1432) requires exact chain/count continuity
   and verify_recovery against the external durable high-water mark. A prefix
   below that mark fails recovery. Cryptographic hashing, signature
   verification, and the external store are not implemented; the required
   callbacks are interfaces only, so rollback safety is not established for
   production.

2. **Node A versus node B identity conflict.** The pre-transition identity
   check is in apply_event_locked near lines 1111-1128. It calls
   durable_identity_fault (src/release_evidence.c:967-1013), which stores both
   the trusted generation identity and the conflicting event identity.
   durable_event_conflict_fault (src/release_evidence.c:1018-1070) handles
   conflicting event IDs across generations. mark_fault_state
   (src/release_evidence.c:862-914) blocks both, while observe and
   re_store_admission_gate mask or reject them. Without a trusted original
   generation, only the authenticated event identity can be quarantined; the
   store remains fail-closed. The conservative scope may reduce availability.

3. **Negative finalization and partition.** event_shape_valid
   (src/release_evidence.c:372-426) rejects negative or partition signals on
   START and requires a signal on NEGATIVE_EVIDENCE. The transition at
   src/release_evidence.c:761-775 converts negative or partitioned
   FINALIZATION_REQUESTED into a committed UNKNOWN transition, so it cannot
   be treated as normal wait. Untrusted malformed fields cause global
   recovery failure, so all existing usage stays held and admission remains
   UNKNOWN.

4. **Admission atomicity.** re_store_admission_gate
   (src/release_evidence.c:1657-1674) is only a mutex-protected predicate.
   It does not reserve a GPU or participate in Slurm resource selection. The
   design requires queue-before-selection and one durable scheduler/device-lease
   transaction; that integration, real authenticated transport, physical
   cleanup verifier, and controller implementation are absent. No production
   guarantee is claimed.

5. **COMMIT and fsync failure.** append_event_record and
   append_commit_record (src/release_evidence.c:489-544) surround the required
   external witness. Replay compares the observed durable-record count and
   chain with verify_recovery. If COMMIT is lost after the witness advanced,
   replay rejects the shorter journal; if COMMIT is present, the external
   high-water record must agree. Chained fault records use the same witness
   path. If the witness itself is unavailable, contradictory, or not truly
   durable, recovery remains blocked. The source has no implementation that
   can prove this callback contract, so the ambiguity remains an explicit
   enablement blocker.

6. **Blocked recovery, pending PREPARE, and index exhaustion.** Replay
   rejects any unmatched PREPARE and never silently truncates it. The
   documented recovery process preserves evidence, independently verifies the
   external high-water and chain, and creates a new externally anchored epoch
   only after audit; the prototype offers no tail deletion or barrier-clear
   function. The node barrier therefore has no unsafe in-process clear path.
   The fixed 256-entry index still freezes at capacity; safe tombstone or
   snapshot compaction is not implemented and remains an onboarding blocker.

## Security boundary and remaining work

The output remains disabled, has no Slurm integration, no real authenticated
transport, no real external high-water journal, no cryptographic chain
implementation, no physical GPU cleanup proof, no scheduler atomic
allocation path, no recovery tool, and no safe idempotency compaction.
Existing tasks remain running and usage-held on UNKNOWN. A blocked request must
be queued before GPU allocation by a future scheduler integration.

Only the two authorized C11 syntax checks were run with warnings as errors,
pedantic mode, and pthread declarations for RELEASE_EVIDENCE_ENABLED=0 and
=1. No tests, simulation, deployment, service restart, installation,
credential access, active-job operation, or commit was performed.
