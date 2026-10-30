# Recovery readiness and projected admission checks

`recovery-ready-fix.patch` applies after `eb0fd27`. Opening a readable WAL no longer suffices to authorize admission, grants or tier snapshots: the ledger must also complete Slurm job-state reconciliation and durable settlement. Failures and disable/finalization clear readiness. Before selection, both real and speculative admission checks project currently running usage at the check time; the locked final admission still durably settles and checks again.

This is a source-only safety correction, not a complete recovery proof. In particular, a complete rollback of the ledger to an earlier valid prefix remains undetectable, unsupported job shapes fail closed, and the full controller still lacks scheduler tier ordering, authenticated grant transport, linking/ABI evidence and behavioral validation. The gate must remain disabled. No tests or training jobs were run.
