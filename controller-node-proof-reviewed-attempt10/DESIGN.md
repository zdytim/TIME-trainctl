# u20 trusted release evidence prototype — review 9

## Status and scope

This is an independent review artifact for a minimal single-node `gpu:1`
allocation lease. `RELEASE_EVIDENCE_ENABLED=0` remains the default and the
source has no Slurm caller, slurmd transport, verifier implementation, GPU
collector, service unit, installation step, or deployment path. It is not
production-ready and does not claim that a monthly gate is enabled.

The strict allocation linearization problem is still open. The prototype
cannot make a scheduler allocation, device lease, cleanup decision, and
durable controller commit one atomic operation. A production caller would
need one controller transaction or lock domain for that sequence. The
admission function in this artifact is only a fail-closed library gate until
such an integration exists.

## Trust boundary

Only a privileged node-side slurmd integration using an existing authenticated
Slurm IPC path may supply an event. The event verifier must bind every field,
including allocation generation, node, boot ID, authorized peer, monotonic
sequence, cgroup identity, device lease token, race checks, evidence flags, and
event ID. User processes, user-owned files, epilog exit status, PID lists, and
an unauthenticated TCP endpoint are not trusted inputs.

The callbacks are deliberately incomplete interfaces. `verify_event` checks
the authenticated node envelope, `verify_admin` checks a separately audited
administrator identity, `verify_anchor` checks the external initialization
anchor, and `verify_commit` checks the controller's external durable commit
witness. No callback in this artifact creates credentials or proves a GPU
operation.

## Secure initialization and journal ownership

`re_store_initialize` is the only creation path. It requires an externally
validated, nonempty initialization anchor and creates the final journal with
`O_CREAT|O_EXCL|O_NOFOLLOW`, mode `0600`, regular-file ownership by the
effective controller user, and a parent-directory `fsync`. Parent components
are opened with `O_NOFOLLOW` and must be directories without group or other
write permission. An exclusive nonblocking `flock` protects the journal from a
second process.

`re_store_open` never uses `O_CREAT`. A missing path returns
`RE_NOT_FOUND`; an empty file or a file without the exact authenticated
initialization record returns `RE_UNINITIALIZED`. Recreating a missing
journal is safe only when the external anchor verifier proves that this is a
new, authorized initialization for the exact controller/node identity. The
code cannot make that external claim itself. If the anchor or its verifier is
lost, recovery stays blocked; an operator cannot recover an existing node by
creating an empty log.

The open path verifies regular-file type, owner, exact mode, secure parents,
and the exclusive lock before replay. The store must complete replay and
anchor verification before `apply` or successful state publication.

## Durable protocol and visibility

Every accepted event is staged as:

1. a length-bounded authenticated `PREPARE` record, followed by `fsync`;
2. an external `verify_commit` check bound to the allocation, event ID, and
   node sequence;
3. a `COMMIT` record, followed by `fsync`;
4. in-memory generation, sequence, and idempotency publication under the
   process mutex.

The file lock serializes other processes and the mutex serializes threads.
Observers cannot see a newly released state before the local commit record and
the external witness have succeeded. A failed witness or append enters a
durable fault path when possible and freezes the store. A prepare without a
commit, a truncated record, or an ambiguous replay is rejected as recovery
failure and remains fail-closed.

Filesystem `fsync` does not provide an independently verified remote storage
proof, and an error after a complete commit write is inherently ambiguous to a
library. The external witness and replay protocol narrow this boundary, but do
not remove it. Therefore the prototype must remain disabled until the
controller's real durable journal contract, recovery procedure, and fault
injection evidence are supplied.

## State and event semantics

`RE_RUNNING` retains the live allocation and usage ownership. `RE_RELEASED`
is the only state that can permit reuse. `RE_UNKNOWN` always retains task and
usage ownership. Missing evidence, a partition, node restart, negative
evidence, cgroup or device mismatch, PID race, verifier failure, or recovery
failure never releases a GPU.

`FINALIZATION_REQUESTED` is a normal wait transition: it records that
finalization was requested, leaves the generation `UNKNOWN` with usage held,
and returns `RE_OK`. It does not set the node-wide blocker. A later positive
cleanup event for the same exact generation can proceed. Actual negative,
restart, ordering, authentication, conflict, or persistence faults return an
unknown/failure result and persist a node barrier where possible.

`RE_EVENT_ALLOCATION_STARTED` rejects negative evidence, controller-partition
flags, administrative approval, missing cgroup identity, and missing device
lease token before it can create `RUNNING`. All event kinds require a bounded
NUL-terminated cgroup field; start, cleanup, and administrative recovery also
require a nonempty path and nonzero identity. Disk records validate the same
constraints before any comparison or transition.

Positive cleanup requires the recorded cgroup path and inode, exact device
lease token, complete before/after privileged process scans, PID start-time and
PID-reuse checks, cgroup identity and emptiness, completed cgroup cleanup, and
positive release confirmation for the exact GPU device. The implementation does
not infer this from `/dev`, a deleted cgroup directory, process exit, or a
normal epilog.

## Generation, replay, and node barriers

The durable sequence is rebuilt per `(node, boot_id)` from committed events,
not per allocation. New events require a strictly greater sequence. The
bounded 256-entry index spans all allocations and generations, so old retries
remain recognizable. Equal event IDs with equal authenticated fields are
idempotent; a same-ID conflict, allocation mismatch, sequence violation, or
index exhaustion freezes the store. No unsafe eviction or compaction is
implemented. A production design needs durable tombstones or an externally
anchored snapshot/compaction protocol with a proof that old retries and
conflicts remain detectable. Until then, capacity exhaustion is a deliberate
availability block.

Authenticated negative evidence and `NODE_RESTART` are normal fail-closed
records during replay, distinct from malformed journal corruption. A restart
creates a persistent node-wide barrier for its node and boot identity; the
node blocker is also checked by node name, so old released generations cannot
be observed as reusable after any persisted barrier. All related old leases
remain running or unknown and usage-held. New allocation admission returns
unknown while the barrier exists.

`re_store_admission_gate` is a serialized, fail-closed predicate for a
production scheduler to call. It is not an atomic Slurm allocation gate by
itself. A real integration must hold the controller's allocation/device-lease
transaction lock, verify the node barrier, reserve the device, append and
commit the START event, and publish the scheduler allocation in one durable
ordering domain. This source has no such integration. An old `RE_RELEASED`
state is masked to `UNKNOWN` by `observe` whenever its node is blocked.

## Administrative recovery

Administrative recovery is a separate authenticated event. It must still
match the stored allocation generation, node, old boot ID, authorized slurmd
peer, cgroup identity, and exact device lease, and it must carry the same
positive cleanup evidence. `verify_admin` only authenticates the authorized
administrative action; it never substitutes for node cleanup proof and never
overrides the stored identity checks. Recovery may resolve an exact lease
record, but it does not clear the node barrier. The barrier therefore cannot
be used to reuse a GPU until a future audited controller procedure establishes
a new trusted node identity and evidence chain.

## Local capability boundary

The companion capability record is based only on read-only local observations.
It found legacy cgroup `devices` and `pids` visibility, a read-only cgroup v2
mount, and a readable boot ID, but could not establish the configured Slurm
cgroup/GRES plugin, privileged slurmd policy, physical GPU inventory, or a
trusted cleanup witness. Those unknowns are intentional blockers. No claim of
actual cgroup isolation, process attribution, physical GPU reset, or Slurm
atomic persistence is made.

## Review deliverables

`REPORT.md` maps the nine review findings to source and design changes and
lists the remaining real-environment gaps. `Makefile` contains compile-only
targets; no tests, simulation, daemon restart, installation, service change,
or runtime data is included. The output manifest covers only the review
artifacts.

