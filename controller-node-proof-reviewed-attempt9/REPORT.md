# Repaired node cleanup prototype review artifact

This is a new sibling revision. The previous release-evidence-prototype output
is preserved unchanged.

The repaired source remains disabled with RELEASE_EVIDENCE_ENABLED=0 and is not
integrated with Slurm. It adds secure parent traversal and journal checks,
0600 effective-user-owned regular-file validation, O_NOFOLLOW/O_EXCL creation,
parent fsync, exclusive flock, pthread serialization, durable-before-visible
state publication, recovery-before-apply, global per-node/boot sequence
tracking, bounded journal-rebuilt idempotency, and durable fail-closed fault
markers.

Normal UNKNOWN transitions replay as fail-closed state. Journal corruption or
replay authentication failure is a separate recovery error. Unknown, negative,
partition, restart, missing-evidence, ordering, and conflict conditions block
new allocations for the affected node while existing usage remains held.

No Slurm transport, privileged slurmd evidence collector, GPU cleanup proof,
tests, deployment, service changes, credentials, binaries, runtime data,
symlinks, upstream source, or unrelated files are included. This remains a
review prototype and makes no trustworthy production proof claim.
