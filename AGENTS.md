# Training platform change control

All training-platform changes must be committed before deployment. Stage explicit project paths only; this workspace contains unrelated projects and private connection material.

The GitHub remote is origin (zdytim/TIME-trainctl). Name each development branch for the actual target or reason it differs, without a codex/ prefix: for example, u20-slurm19 for the u20 Slurm 19 adaptation, and pro6000d-slurm26 for its distinct Slurm 26 adaptation when that work starts. Switch branches only when the work changes target or purpose; a branch name is not evidence of deployment or completion. Push committed changes to the matching branch, preserve main as the baseline, and record each deployed commit.

Record each release's Git commit, target host, timestamp, changed components, backup location, verification evidence and rollback outcome in deployment records. Never describe a staged or disabled quota feature as active.

Do not commit passwords, tokens, private keys, databases, server backups or runtime logs. Do not run tests unless the user explicitly requests them. Syntax and deployment health checks remain permitted.

Large implementation work uses the u20-dispatch skill. Preserve the strict requirement: exhausted users' unstarted jobs remain queued without any GPU allocation; existing running jobs finish and accrue overage.
