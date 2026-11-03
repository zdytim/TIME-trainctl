# Slurm 26 candidate: initial witness descriptor preservation

Slurm 26 `main` closes inherited descriptors before reading configuration. The disabled quota candidate had a validator for a root-owned private witness descriptor but did not call it, so an initial launch through the intended root launcher would discard the descriptor before ledger recovery.

`slurm26-preserve-witness-fd-draft.patch`, applied with `git apply --unidiff-zero` after `slurm26-controller-lifecycle-draft.patch`, changes only the disabled experimental build: on the original controller's initial start it retains the validated witness through the close-all operation. Invalid or absent descriptors follow the existing close-all path, and Slurm's script helper does not retain the descriptor. The validator sets close-on-exec, so ordinary child programs cannot inherit it.

This addresses **initial startup only**. Slurm 26 successful reconfiguration forks and execs a new controller, and both `closeall_except` in `_try_to_reconfig` and close-on-exec discard this descriptor. The child would therefore fail quota recovery; a safe authenticated handoff or a separately designed restart path is still needed. This patch and the launcher remain disabled and are not deployable. No production service was changed, no tests were run, and only static inspection and reverse patch applicability were performed.
