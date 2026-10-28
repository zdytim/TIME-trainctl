# Training platform change control

All training-platform changes must be committed before deployment. Stage explicit project paths only; this workspace contains unrelated projects and private connection material.

Record each release's Git commit, target host, timestamp, changed components, backup location, verification evidence and rollback outcome in deployment records. Never describe a staged or disabled quota feature as active.

Do not commit passwords, tokens, private keys, databases, server backups or runtime logs. Do not run tests unless the user explicitly requests them. Syntax and deployment health checks remain permitted.

Large implementation work uses the u20-dispatch skill. Preserve the strict requirement: exhausted users' unstarted jobs remain queued without any GPU allocation; existing running jobs finish and accrue overage.
