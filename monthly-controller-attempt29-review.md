# u20 controller attempt29/18 follow-up

Static review of outputs/strict-monthly-quota-19.05.5-u20-attempt18 confirms strict gate remains disabled. Preserve this safety boundary. Fix or document two blockers:

1. Reconfigure invalidate C:936–948 unconditionally marks mq_healthy=false and mq_ambiguous=true; success reconcile C:873–934 never restores either, leaving all future GPU work permanently frozen after any reconfigure. Separate recovery-in-progress from persistent unhealthy/unknown, and only resume after verified mapping, WAL, allocation and evidence are all consistent. Never blindly clear genuine ambiguity.
2. RESOLVE_UNKNOWN C:1353–1387 accepts root input without validating credible time, count, identity, trusted node evidence, audited actor or operation id. Restrict/disable resolution until explicitly designed with trustworthy proof and idempotent durable audit; no administrator string is proof of actual GPU cleanup.

Cutover UNKNOWN and NULL TRES/reconfigure improvements found but require static verification. Core strict physical allocation, rollback and bounded IO remain unproven; keep disabled. No tests, training, service changes or deployment; isolated compilation/static only, sibling outputs. Exact Slurm19 sources; Git/rollout only by supervisor.
