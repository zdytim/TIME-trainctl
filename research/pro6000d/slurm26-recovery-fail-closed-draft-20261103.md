# Slurm 26 candidate: fail closed on ambiguous recovery

The disabled draft's `monthly_quota_reconcile` could turn a recovered PREPARE into a COMMIT based on a matching job number and GPU count, even though that does not establish when allocation occurred. Its selectable-tier branch also did not compare the recorded submission timestamp or restart count, and a completing allocation could pass without proof of resource cleanup. The function is not connected to the running controller, but it would be unsafe to use as a recovery hook in that form.

`slurm26-recovery-fail-closed-draft.patch` is a disabled candidate change on top of `slurm26-reservation-draft.patch` and `slurm26-aborted-reservation-draft.patch` (apply with `git apply --unidiff-zero`). It rejects unresolved PREPARE and COMPLETING records, and checks recorded submission and restart identity for selectable-tier allocations. It removes the recovery path that fabricated COMMIT events. Ambiguous recovery blocks new admission rather than interrupting existing tasks.

This does not make recovery complete: trusted resource-release evidence, durable ledger lifecycle, remaining allocation integration and normal operator recovery are still required. No production service or running job was modified; only static review and a reverse patch applicability check were performed, without tests.
