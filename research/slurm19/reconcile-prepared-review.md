# Recovery of interrupted allocation intents

The draft reconciliation called `_settle_all` before resolving PREPARE records; this could append SETTLE for an uncommitted allocation, but WAL replay rejects such SETTLE events. It also automatically recorded ABORT if the recovered job record was missing or belonged to another user, even though that absence does not prove resource allocation never occurred. It appended COMMIT before checking the recovered GPU count, and treated an unsupported running-job shape like a job without GPUs.

`reconcile-prepared-fix.patch` applies after `b9f9f9b`. It validates all recovered allocation and running/suspended job records first; absent or mismatched jobs, unsupported shapes, mismatched GPU counts and uncommitted COMPLETING jobs freeze new admission. Only after validation does it commit recoverable PREPARE records, followed by settling intervals. This can conservatively freeze on unsupported non-GPU job shapes; availability and operator recovery remain unresolved.

This does not prove journal integrity, physical resource release, exact COMPLETING charges, or durable agreement between Slurm allocation and WAL. No trusted high-water mark, normal audited bootstrap, complete recovery workflow or tests exist. The gate stays disabled and no service or jobs were changed.
