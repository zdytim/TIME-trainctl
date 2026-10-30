# Resource selector ambiguous failure handling

`selector-ambiguous-fix.patch` follows the staged controller patch. If a select plugin fails during job begin, or succeeds without providing node resources, the controller no longer records a clean quota ABORT without evidence that the plugin rolled back every resource side effect. It keeps the durable PREPARE and marks the ledger unhealthy, blocking further new governed GPU allocations until recovery and review. Existing allocations are not intentionally stopped.

The u20 `select/cons_tres` 19.05.5 source describes `select_p_job_begin` as unused and returns success, but other select implementations and failure/crash paths require separate proof. This conservative change does not prove that all selector, GRES or allocation side effects are covered, and may reduce availability on error. No tests or production changes were made.
