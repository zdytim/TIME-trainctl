# u20 monthly cutover preflight

On 2026-10-31, a temporary unit composed from u20's existing service and the
tracked `f8dd428` candidate override was checked with `systemd-analyze verify`.
The command returned zero and did not report a candidate-unit parse error, but
printed unrelated host unit/varlink warnings; it did not start the candidate or
prove actual systemd startup or rollback. The temporary file was removed.

The read-only preflight from `5f72d96` was then run on u20 as root from a
hash-verified, root-owned temporary directory. It verified the pinned baseline
and candidate configuration, launcher, broker, service files, all 99 staged
release members, current old-controller PID/executable and service status,
active cgroup settings, empty Slurm queue, terminal platform jobs, absence of
bound approvals or unresolved grants, absence of prior quota ledger/override,
and SQLite `quick_check`. It reported seven terminal historical jobs,
`fenced=false`, and `activated=false`. The first version refused the private
database because its group was not root; inspection showed root ownership,
group 998 and mode 0600. The amended check accepts a non-root group only for a
private root-owned database, and has three local permission tests.

Afterward the temporary code was removed, the queue remained empty, Slurm and
training services were active, and `SlurmctldParameters` remained unset. This
was only a point-in-time, read-only check. It does not fence submissions, take a
coordinated cutover backup, create a ledger, clear history, start the candidate,
or establish production readiness. A later cutover must recheck all state under
its own maintenance fence and account for interruption and rollback.
