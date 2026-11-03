# Slurm 26 candidate: aborted reservation generation

The disabled draft creates a `PREPARE4` allocation generation and links it to the bound reservation. Both runtime `ABORT` and WAL replay removed the temporary active allocation but left that generation attached to the reservation. A subsequent prepare requires the generation to be zero, so this permanently blocks retry of the same bound request after a clean abort.

`slurm26-aborted-reservation-draft.patch` applies with `git apply --unidiff-zero` on top of `slurm26-reservation-draft.patch`. It records in memory whether a `PREPARE4` owns the reservation and, after a durable abort, clears only the matching reservation generation while retaining the reservation and its job binding. Replay reconstructs the same flag and performs the equivalent transition; a missing or mismatched reservation fails closed. This does not prove GPU release: the abort is valid only for a path already known to have had no allocation side effects.

The remainder of the allocation transaction, node cleanup evidence, recovery, concurrent accounting and selected-tier lifecycle is still incomplete. This patch is a disabled source draft, not deployed or ready to enable. Only static inspection and reverse patch applicability were performed; the pristine source extraction lacks a configured build environment, and no tests were run.
