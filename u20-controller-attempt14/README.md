# u20 strict monthly controller package

Review `implementation-report.html` first. This is a reviewable Slurm
19.05.5 source patch and isolated compile artifact. Strict enforcement is
intentionally disabled: the 19 scheduler hook has no proven resource-commit
linearization point and the synchronous WAL/fence path has no bounded-I/O
proof. Explicit enablement fails closed; this package is not deployable.

The patch is generated against the verified clean `slurm-19-05-5-1` source and
is separate from the preserved Slurm 26.05.5 pro6000d work. Use
`docs/COVERAGE-AND-LIMITATIONS.md` for the exact residual blockers and
`docs/BUILD-EVIDENCE.md` for the evidence boundary. Activation remains a
separate administrator action after review and a Git commit, but this revision
will refuse activation until the blockers are resolved.

The backend and frontend retain compatibility material only. They do not grant
allocation permission, and no cached API decision substitutes for a controller
decision.
