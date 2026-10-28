# u20 boundary and practical risk

No production configuration, credentials, services, scheduler state, or jobs were accessed. Review facts supplied by the supervisor remain: u20 uses `accounting_storage=none`, GPU was absent from `AccountingStorageTRES` at the last check, and the isolated build could not find PAM or munge libraries. Those facts are not independently verified here.

The practical risks are:

- A select/GRES transaction patch must match the installed Slurm19 controller and every loaded select/GRES plugin ABI. The existing attempt20 builder compiles only `slurmctld`; it does not establish a production plugin build.
- Without a typed GPU TRES map initialized before recovery, a missing position cannot be treated as CPU zero.
- Without a trusted cleanup callback after device/container release, releasing quota early can undercharge a user forever.
- A synchronous WAL under scheduler locks risks unbounded scheduling stalls; an asynchronous WAL risks physical allocation before durable admission.
- Preemption, backfill, immediate allocation, requeue, resize, reconfigure, node failure, and controller recovery must all use the same transaction or be fail-closed.

These are deployment blockers, not claims about the live host.
