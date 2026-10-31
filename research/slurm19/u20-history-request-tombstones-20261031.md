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
standalone production purge. Critically, the currently installed/older broker
does not consult tombstones: after a purge, rolling back to that broker while
allowing submissions could recreate old requests. A release/rollback procedure
must preserve a tombstone-aware admission path or keep submission closed until
reconciled; do not run the purge until that and the remaining cutover safeguards
are in place. Requests originally submitted without an ID have no identity to
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
