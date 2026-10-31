# Preserving request identities when clearing training history

On 2026-10-31 a read-only query of u20's platform database found seven
completed/failed jobs with client request IDs, including two rows with request
fingerprints. `job_request_fingerprints.job_id` references `jobs.id` with
`ON DELETE CASCADE`. Deleting the jobs also deletes their fingerprints and the
unique `(owner_uid, client_request_id)` entries. A later retry of an old ID
could then submit a new job. These counts describe that instant only; no data
was deleted.

Candidate `bc56fb1` adds an independent `job_request_tombstones` table and
checks it before submission and again inside the insertion transaction. A
separate, non-CLI database primitive inserts these minimal identity pairs
transactionally before deleting terminal, finished jobs, with strict checks
for the known schema, approval links, triggers and foreign keys. It leaves
approvals and audit rows intact. Tests on private SQLite databases covered
preserving audit, rejecting ambiguous or active records, old-ID retry,
per-user isolation and a tombstone inserted between preflight and transaction.
The candidate broker reconstructed from committed source had SHA-256
`24afd69cff5eac856f2ccaaf8a97b95c1823c911c83f40338745cd4ab813e821`.

This primitive does not stop services, fence submissions, back up the database,
inspect the scheduler, clear jobcomp or coordinate crash recovery; it is not a
standalone production purge. The currently installed/older broker does not
consult tombstones directly. The initial candidate therefore could not safely
roll back while accepting submissions; the database-level mitigation and its
scope are described below. Do not run the purge until the remaining cutover
safeguards are in place. Requests originally submitted without an ID have no identity to
preserve. Existing application links, unknown schema changes or active jobs are
reasons for the primitive to refuse. No quota controller or cleanup is enabled.

On 2026-10-31 approximately 12:20 Asia/Shanghai, `b1f2597` copied the live
SQLite database through SQLite's backup API into root-owned mode-0600 files in
`/var/backups/pro6000d-training/shadow-history-k04ky4ku` (directory mode 0700).
The hash-verified candidate broker initialized a separate copy; the purge core
deleted seven shadow jobs, preserved all seven request identities, rejected
each old request on the candidate broker, and left other existing table schemas
and row digests unchanged. The shadow database passed `integrity_check`.
Afterwards the live database still contained seven jobs and seven request IDs,
with no tombstone table; Slurm and the backend were active, the queue was
empty, and the monthly parameter was unset. This is a point-in-time shadow
check, not a coordinated maintenance backup, live deployment, jobcomp cleanup,
or proof that the old broker safely handles tombstones.

The later `36b09a9` purge core adds SQLite INSERT and UPDATE triggers in the
same transaction as the tombstones and job deletion. In a private SQLite
fixture, the candidate and the old broker both failed to recreate a purged
request; the old broker received a SQLite integrity error, not the candidate's
user-friendly 409 response. An injected constraint failure rolled back both
the triggers and the job deletion. The insert uses conflict-targeted handling
and verifies all request IDs survived; it does not silently ignore unrelated
constraint errors.

On u20 at approximately 12:24, the updated shadow tool repeated the check on a
fresh protected SQLite backup at
`/var/backups/pro6000d-training/shadow-history-tcy4xvlq`. All seven IDs were
rejected by both the candidate and a copy of the currently installed broker
(SHA-256 `4224e6aa116532585c6c9e837ee102e9d508ef00b04e6e685bd5e6f092e88821`);
the other table contents and schemas were unchanged and SQLite integrity was
OK. Scheduler calls were stubbed. These triggers protect only while that same
database retains both triggers and tombstones. Restoring an older database,
dropping or modifying either, or writing through a different database can
remove this protection; rollback still needs explicit data reconciliation and
must not overwrite later approvals or quota accounting. The production database
was not modified and the controller remains disabled.
