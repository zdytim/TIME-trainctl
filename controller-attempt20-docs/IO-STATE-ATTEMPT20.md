# Durable state and scheduler I/O audit

The patched ledger uses a journal plus a clock fence. `_append_locked` writes and fsyncs the journal, then stores the fence. `_checkpoint_locked` writes a temporary checkpoint, fsyncs it, renames it, and fsyncs the state directory. These operations run while `mq_lock` is held. Admission now treats checkpoint failure as a failed decision and leaves the durable intent for recovery; commit likewise marks the authority unhealthy and ambiguous if its WAL or checkpoint fails.

This is conservative crash handling, not a bounded I/O proof. Filesystem fsync latency is not bounded by the Slurm scheduler contract. A background queue would improve scheduler latency only if the scheduler waited for the durable token before any physical side effect; allowing it to proceed would reintroduce the allocation race. A bounded queue that can reject before side effects is possible only with an explicit backpressure contract and a proof that all resource consumers honor it.

Initialization remains explicit: disabled mode does not inspect or create monthly state; explicit enablement requires an administrator-created marker and a secure state directory, then rejects because `MQ_STRICT_INTEGRATION_PROVEN` is zero. Journal creation uses exclusive creation and inode plus parent-directory durability. A marker with a missing/corrupt journal is treated as lost state and fails closed; old files are not replaced or deleted. Clock-fence replay and realtime regressions poison health/ambiguity.

The scheduler allocation path still cannot be made strict by moving an fsync earlier. The feature remains disabled until a transaction boundary with bounded crash semantics is available.
