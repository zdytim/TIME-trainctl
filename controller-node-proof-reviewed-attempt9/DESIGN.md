# Design record

## Scope and disabled status

The target is a minimal single-node `gpu:1` allocation release record. This
prototype is not feature complete and is not enabled. It has no production
caller, no controller plugin, no slurmd plugin, no new endpoint, and no cleanup
operation. The compile-time integration switch is `RELEASE_EVIDENCE_ENABLED=0`.
The repository was empty when this sibling artifact was created, so there is no
older source iteration here to modify or delete.

The unresolved strict allocation linearization problem remains explicit: this
library cannot decide whether a new allocation was admitted between the last
cleanup observation and a controller decision. A production implementation
must place allocation admission, device lease ownership, and the final release
decision behind one controller durable ordering point. This prototype never
claims to solve that ordering.

The implementation switch remains `RELEASE_EVIDENCE_ENABLED=0`. The repaired
source is a review artifact only; it has no production caller and cannot open a
journal or apply an event in the disabled build.

## Trust boundary

Only a privileged node-side `slurmd` integration can produce a release event.
It must use the existing secure Slurm IPC path and provide an authenticated
peer identity, node boot ID, allocation generation, and node monotonic
sequence. `re_event_verifier` is the narrow integration point for verifying
that envelope. The default verifier rejects every event. The library does not
trust a job process, user-owned file, epilog status, PID list supplied by a
user, or an unauthenticated TCP request.

The verifier must bind the authenticated bytes to all fields in the event,
including the allocation key, node, boot ID, peer identity, sequence, evidence,
and event ID. An implementation may use the Slurm authenticated message
identity or a signed envelope. The library stores the authenticated bytes in
the journal and asks the verifier to check them again during replay.

The verifier and transport are deliberately interfaces. This repository does
not provide Slurm IPC marshalling, credentials, a node daemon, or GPU cleanup
logic. No local process observation can satisfy the verifier.

## Journal security and recovery

`re_store_open` requires an absolute journal path whose every parent component
is opened with `O_NOFOLLOW`, is a directory owned by the effective controller
user or root, and is not group- or other-writable. The final file is created
with `O_CREAT|O_EXCL|O_NOFOLLOW`, mode `0600`, and the effective user as owner;
existing files must pass the same regular-file, owner, and mode checks. The
parent directory is `fsync`ed after opening, and the journal holds a
non-blocking exclusive `flock` for the store lifetime. There is no implicit
state-directory creation. A deployment must provision that directory with the
controller's trusted ownership policy.

The store initializes its mutex and completely replays the locked journal
before returning a usable handle. `apply` is rejected until recovery succeeds.
`apply`, `observe`, and replay are serialized by the mutex; the file lock
serializes separate processes. A transition is staged, appended, and `fsync`ed
before the in-memory generation can become `RE_RELEASED` or otherwise publish
the new event. An append or post-append bookkeeping failure freezes the store
and exposes only `UNKNOWN`.

The journal contains authenticated event records and controller-local fault
records. Fault records durably preserve fail-closed decisions for authentication
failures, ordering violations, and conflicting event IDs. Truncation, malformed
records, or replay authentication failure is a journal recovery failure and
does not become a normal cleanup `UNKNOWN`; callers must keep all usage held and
freeze new allocations until an audited recovery process repairs the journal.

## Evidence contract

`RE_EVENT_CLEANUP_ATTESTED` is accepted only when all of the following are
true:

* the allocation generation was started by a verified event;
* the node and boot ID match the generation; a boot change cannot complete an
  old generation;
* the node sequence is greater than the last accepted sequence for that boot;
* the cgroup path and inode or equivalent stable identity match the recorded
  allocation cgroup;
* the verifier-bound device lease token matches the allocation's exact GPU
  lease;
* the privileged scan is complete, records before/after scan tokens and counts,
  checks each process PID together with its `/proc/<pid>/stat` start-time value,
  and checks the cgroup identity again;
* the final process scan is empty and `pid_reuse_checked` is true;
* cgroup cleanup has completed and the trusted node reports positive device
  release for the exact devices leased to this generation;
* the evidence contains no negative, partial, stale, or contradictory result.

The code records evidence as an authenticated opaque envelope. It intentionally
does not infer GPU release from `/dev`, process exit, `devices.list`, a deleted
cgroup directory, or the absence of a completion RPC. The slurmd integration
must define how its configured cgroup/device plugin proves exact device
release. If that plugin cannot prove it, the result is `RE_UNKNOWN`.

## State and failure behavior

`RE_RUNNING` means the generation is live or its final state is not known.
`RE_UNKNOWN` means the controller must retain task and usage ownership. It is
the result for missing final evidence, controller/node partition, restart,
negative evidence, PID race, PID reuse uncertainty, cgroup mismatch, device
evidence failure, and verifier failure. `RE_RELEASED` is the only state that
permits reuse. There is no implicit timeout-to-release.

Events are idempotent by a bounded, journal-rebuilt 256-entry identity index
that spans allocations and generations. A
duplicate with the same authenticated fields is a no-op; the same ID with
different content or allocation identity creates a durable fault. The node
sequence is tracked globally per `(node, boot_id)`, not per allocation, and
must strictly increase for every new event. Old retries are recognized before
sequence validation. Index or tracker exhaustion freezes the store rather than
evicting evidence. The journal is length-prefixed, append-only from the
library, written with full writes, and `fsync`ed before state publication.

Normal authenticated negative evidence, node restart evidence, and sequence
fault markers replay as fail-closed `UNKNOWN`. Journal corruption and replay
authentication failure return a separate recovery error and prevent the store
from becoming usable.

Any persisted `UNKNOWN` transition blocks new allocation starts for that node
and boot identity; a node restart or node-wide partition/negative fault keeps
the node blocked across replay. Existing generations remain running or
unknown and their usage stays held. The block is never cleared implicitly;
only a separately audited administrative process can decide whether a future
controller revision may clear it.

An administrative recovery event is separately authorized and audited. It must
carry the same complete positive cleanup evidence and a verifier-approved
administrative identity. It is not a bypass for missing evidence and it never
permits an operator to assert that cleanup occurred.

## Event ordering and partitions

The node sequence is a logical ordering supplied by the trusted node channel;
the controller also records its local arrival order in the journal. Wall-clock
time is informational and is not used for safety. A lost final event or a
controller/node partition leaves the generation running or unknown. A normal
epilog completion or container deletion does not change that result.

The journal is controller-local durable state. A production deployment must
replicate or otherwise protect it according to the controller's existing
durability and authentication design before using it for resource reuse. This
prototype does not implement replication, key storage, key rotation, Slurm IPC
marshalling, or the actual trusted cgroup/GPU evidence collector.

## Files

* `include/release_evidence.h` defines the explicit evidence and verifier API.
* `src/release_evidence.c` implements disabled-by-default state transitions,
  authenticated journal append, replay, generation checks, PID/cgroup race
  gates, and audited recovery.
* `Makefile` runs compile-only checks.
* `LOCAL_CAPABILITIES.md` records only read-only local observations.
