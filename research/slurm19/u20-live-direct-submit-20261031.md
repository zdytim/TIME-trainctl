# u20 live direct-submission check

On 2026-10-31 at approximately 11:56 Asia/Shanghai, the tracked
`validation/direct-submit-probe.py` (SHA-256
`0a692b7b6eafcb714bba0becaf666291fdbfaf38327c273be35086270a29fae5`)
was copied to u20, checked against that digest, and run as `timzhang`.
It attempted to submit an innocuous GPU job scheduled ten minutes in the
future, with a forged platform-style comment. Slurm rejected the submission
with the expected platform-only message; the script reported PASS. No test job
was accepted or cancelled. The copies made for this probe were removed.

At approximately 11:59 the tracked `direct-interactive-probe.py` from commit
`8218b94` (SHA-256
`e243a834300dfab367b71f57e6c942ea6e00e3869ef00fb608ad79c35e6180bd`)
was verified and run as the same user. Both `srun` and `salloc`, also using
forged platform markers and unique job names with a ten-minute future start,
returned the expected platform-only rejection. The script checked for jobs
under each exact generated name and reported no cleanup failures; its temporary
copy was removed. The queue was empty before and after.

Afterwards `slurmctld`, `slurmd`, and `pro6000d-training` were active,
`/dev/nvidia0` retained `other::---`, and `SlurmctldParameters` was unset.
These observations cover three direct submission commands for this one account
and marker format, not direct device access, other accounts, every Slurm option,
or future permission changes. They do not establish that the monthly quota
gate is active.
