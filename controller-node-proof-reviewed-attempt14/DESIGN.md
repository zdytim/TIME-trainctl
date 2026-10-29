# u20 trusted release evidence prototype — review 10

## Status

This is a new independent review artifact. It remains disabled by default with
`RELEASE_EVIDENCE_ENABLED=0`, has no Slurm integration, and does not enable
or claim any monthly rule. It contains no service, daemon, transport,
credential, GPU collector, or deployment path.

The required scheduler behavior is stronger than a node event or a library
predicate: when a node is blocked or the durable evidence budget is exhausted,
the scheduler must queue the request before selecting or assigning a GPU. This
library cannot make that allocation decision atomic. Existing tasks and usage
remain held while the evidence state is UNKNOWN.

## Anti-rollback anchor and chain

The initialization anchor is only an identifier. It becomes a safety anchor
only when the external verifier binds it to a controller durable record for the
exact node identity, journal epoch, initial chain head, and committed-event
high-water mark. `re_store_initialize` stores the initial chain head and
`re_store_open` requires the same expected anchor and chain head.

Each authenticated event and durable fault record carries a nonzero previous
chain head and record chain head. The source checks that the previous head
equals the recovered head, that each durable-record count is exactly the
previous count plus one, and that the COMMIT or fault record repeats the
event's chain fields. `verify_event` and the external
callbacks must cryptographically authenticate the canonical event and chain
bytes. This source does not implement cryptography and cannot establish that
property by itself.

`verify_commit` is a required external durable witness. Before writing
COMMIT, it must durably record the next committed count, allocation/event
identity, and new chain head in the controller's trusted store. On replay,
`verify_recovery` must compare the journal's observed count and chain head
with that external high-water record and reject a lower prefix, an unknown
epoch, a missing witness, or a conflicting head. A journal reduced to its
initialization record therefore cannot reopen if the external high-water mark
shows committed events.

This protocol handles the two useful crash outcomes: a COMMIT absent while
the external high-water mark advanced is rejected, and a complete COMMIT is
accepted only when it agrees with that same external witness. An fsync error
after a complete COMMIT remains a filesystem-level ambiguity, but a missing
COMMIT cannot silently erase history because the external high-water check
rejects the shorter prefix. If the external witness itself is unavailable or
ambiguous, recovery is blocked. No callback implementation, cryptographic
library, replicated journal, or fault-injection evidence is included here, so
this prototype remains disabled.

## Identity conflict and isolation

A known allocation generation has a trusted node, boot ID, authorized peer,
cgroup identity, and device lease. Before sequence validation and transition,
an event for that generation must match those identities. A NODE_RESTART may
change boot ID only when the node name still matches and its authenticated
event establishes the new node incarnation.

If an event claims node B for a lease trusted to node A, the source writes a
fault containing both the trusted A identity and the conflicting B identity.
Replay blocks both identities by node name and boot identity, and masks old
RELEASED observations. If there is no trusted original generation, only the
authenticated event identity can be quarantined; the external scheduler and
controller must treat the allocation and node evidence as globally UNKNOWN.
This conservative scope can reduce availability, but avoids allowing A's
other released leases or B's admission to continue after an identity
contradiction.

## Event validation and finalization

All event kinds require bounded, NUL-terminated cgroup storage before any
comparison. START rejects negative evidence, controller partition flags, and
administrative approval. START also requires a cgroup identity and device
token. A NEGATIVE_EVIDENCE event must carry a negative or partition signal.
Cleanup and admin recovery require the complete positive cleanup evidence and
reject negative or partition flags. FINALIZATION_REQUESTED is validated in the
state transition: negative, partition, or administrative signals take the
durable UNKNOWN/fault path rather than the normal wait path.

A normal FINALIZATION_REQUESTED with no negative signal records a wait state,
retains usage, returns `RE_OK`, and does not create a node blocker. A
FINALIZATION_REQUESTED carrying negative, partition, or administrative
information becomes a committed UNKNOWN transition with a node barrier and
an UNKNOWN result. If its fields cannot be trusted, the whole store remains
recovery-failed and all admission remains UNKNOWN.

## Durable visibility and recovery

The sequence is staged as PREPARE plus fsync, external durable witness,
COMMIT plus fsync, then in-memory publication under the mutex. The process
lock and journal flock serialize threads and processes. The chain count and
head are updated only after commit publication.

A PREPARE without COMMIT is not repaired by silently dropping the tail. Replay
rejects the journal and leaves the store unusable. The recovery procedure is
external and audited:

1. deny new scheduler admissions before GPU selection and keep running tasks
   and usage ownership unchanged;
2. preserve the original journal and external high-water record read-only;
3. have independent administrators verify record framing, authenticated event
   signatures/MACs, chain continuity, COMMIT count, and the external witness;
4. if a replacement journal is justified, create a new externally anchored
   journal epoch from an independently verified prefix, retain the old journal
   as evidence, and record the node quarantine and review decision in the
   controller audit store;
5. require a new authenticated node boot identity and fresh positive cleanup
   evidence before any GPU can be reused.

The prototype has no truncate, tail-delete, barrier-clear, or journal-repair
API. An administrator event can resolve an exact allocation only with the old
node/boot/peer, cgroup/device identity, admin authorization, and positive
cleanup evidence; it cannot clear the node barrier. This is an intentional
security boundary and an availability blocker until a real audited recovery
tool and controller procedure exist.

The 256-entry idempotency index remains bounded and durable. Exhaustion
freezes the store and queues future work. No safe eviction or compaction is
implemented. Production needs externally anchored tombstones or a snapshot
protocol that proves old retries and conflicts remain detectable.

## Scheduler boundary

`re_store_admission_gate` is a serialized fail-closed observation. It cannot
reserve a GPU or publish a Slurm allocation atomically. A real integration must
hold the controller's scheduler/device-lease transaction, consult the durable
node barrier and high-water state, queue blocked requests before GPU selection,
reserve the exact device, append and externally witness START, and publish the
allocation in one commit domain. A race between this predicate and Slurm's
resource selection is an unresolved safety defect, so this source is not
production usable.

## Node and GPU evidence boundary

The local capability record only establishes visible legacy cgroup devices/pids
interfaces and a readable boot ID. It does not prove configured Slurm cgroup
or GRES behavior, privileged slurmd IPC, process attribution, physical GPU
inventory, device reset, or cleanup. Only a real privileged node integration
with authenticated cgroup/PID/device evidence may produce a releasable state.
No user process, epilog status, `/dev` listing, or deleted cgroup directory
is accepted as proof.
