# Staged recovery check for untracked completing jobs

Reconciliation checked ledger entries against recovered completing jobs but only required a ledger entry for running and suspended jobs. A completing job without a ledger record could therefore pass recovery, even though Slurm may already have discarded its GRES information and the candidate cannot tell whether it used a GPU. Its usage could be missing from the ledger.

This candidate conservatively fails recovery when any completing job has no active ledger record. That includes CPU-only completing jobs: it intentionally favors accounting integrity over availability when their previous resource shape cannot be proven. Existing jobs are not cancelled, but new quota-governed GPU admission stays closed; the present design does not automatically retry reconciliation after that job completes, so an operator recovery path is still required. This is not an automatic backfill or proof that pre-cutover usage is charged.

Source review only; no recovery, crash simulation or behavior test was run, and the live controller was not modified. Do not activate without reviewing migration and recovery behavior.

On u20, commit `a1c5a70`'s patch applied to the isolated incremental candidate and `make -j2 -C src/slurmctld` completed successfully. The candidate ELF SHA-256 is `c6a5db61ae486188c254208eb6383b3f25ccc594a77c0b6dbfceb6d5a97b419b`; existing Slurm and training services reported active afterward. The candidate was not installed or run.
