# Staged scheduler wake after monthly grant

`grant-scheduler-wakeup.patch` follows `cf6edc9`. A controller-confirmed grant updates `last_job_update` under Slurm's configuration-read/job-write locks and queues a scheduler cycle after releasing those locks; this informs ordinary scheduling and backfill that pending jobs may have changed eligibility. A repeated idempotent grant can trigger an extra harmless scheduling request. Snapshot reads still take only the configuration read lock.

This does not guarantee immediate execution, override resource/partition policy, or fix stale tier ordering within an already-running scheduling cycle. Lock ordering, thread shutdown, backfill interruption and grant timing remain untested. The controller is not deployed or enabled. No tests or jobs were run.
