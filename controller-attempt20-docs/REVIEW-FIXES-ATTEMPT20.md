# Attempt20 review fixes

- Admission now returns pending and marks health plus ambiguity when the durable checkpoint cannot be published after `START_INTENT`.
- A commit WAL or checkpoint failure now marks the ledger unhealthy and ambiguous rather than silently treating the allocation as committed.
- Every `select_g_select_nodeinfo_set` error path calls `monthly_quota_abort`. If monthly enforcement were enabled, the existing intent is retained as durable UNKNOWN because Slurm 19 provides no rollback proof. With the strict feature disabled, this helper is a no-op and ordinary scheduler behavior is unchanged.
- `MQ_STRICT_INTEGRATION_PROVEN` remains `0`; initialization rejects explicit enablement.
- The source audit records the preemption, burst-buffer, select/GRES, actual-TRES, completion, and reconfigure paths with exact references.

Residual blockers remain allocation linearization, physical cleanup evidence, bounded durable I/O, runtime recovery validation, production PAM/munge/TRES/ABI validation, and behavioral validation. No blocker is represented as fixed by compilation.
