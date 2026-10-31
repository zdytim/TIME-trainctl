# u20 live direct-submission check

On 2026-10-31 at approximately 11:56 Asia/Shanghai, the tracked
`validation/direct-submit-probe.py` (SHA-256
`0a692b7b6eafcb714bba0becaf666291fdbfaf38327c273be35086270a29fae5`)
was copied to u20, checked against that digest, and run as `timzhang`.
It attempted to submit an innocuous GPU job scheduled ten minutes in the
future, with a forged platform-style comment. Slurm rejected the submission
with the expected platform-only message; the script reported PASS. No test job
was accepted or cancelled. The copies made for this probe were removed.

Immediately afterwards the Slurm queue was empty, `slurmctld`, `slurmd`, and
`pro6000d-training` were active, and `SlurmctldParameters` was unset. These
observations cover direct `sbatch` for this account with this marker, not
`srun`, `salloc`, direct device access, other accounts, or future permission
changes. They do not establish that the monthly quota gate is active.
