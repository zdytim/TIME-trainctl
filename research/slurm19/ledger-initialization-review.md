# Initial monthly ledger provisioning candidate

`u20-training-platform/tools/time-quota-ledger-init.py` provisions only a genuinely new pair of U20MQ1 files. It requires an existing trusted state directory owned by the effective service user, rejects symlink or writable ancestor directories, and uses exclusive 0600 creation, file fsync and directory fsync. It never overwrites, truncates, repairs or removes an existing ledger. Partial initialization intentionally remains for review; the controller rejects incomplete pairs.

Run only under the intended slurmctld identity after independently confirming fresh accounting is appropriate, knowing the current Slurm job state, and reviewing activation/migration. The tool does not verify old usage, installed controller state, running jobs, file-system crash semantics or safely repair partial initialization. No invocation or behavioral tests were performed; the controller gate stays disabled.
