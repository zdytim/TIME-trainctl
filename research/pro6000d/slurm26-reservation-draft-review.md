# Slurm 26 reservation draft packaging review

This is an incomplete disabled draft from u20 dispatch `j-9c99bdd6f97c47cab772` attempt 36; it must not be installed or enabled. The audit-exported source artifact reported SHA-256 by its dispatcher manifest and is preserved in the collection directory.

The original controller patch fails `git apply --check` on the verified 26.05.5 upstream archive: its final three existing upstream files were incorrectly encoded as additions from `/dev/null`. The repacked patch preserves the emitted candidate contents for those three files but represents them as diffs against the original archive. It applies cleanly to the verified archive; this is a packaging check, not a code-safety or behavioral verification. No tests were run.

Allocation gating, lifecycle accounting, trusted release evidence and recovery remain incomplete. Default guards remain disabled. The companion broker candidate has separate critical findings in `broker-reservation-review-20261103.md` and must not be deployed.
