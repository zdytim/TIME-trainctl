# Mandatory audit fixes

| Finding | 19.05.5 result | Evidence boundary |
|---|---|---|
| Journal field count and cross-month charging | Writer emits one event plus ten fields; replay requires exactly ten and rejects trailing fields. Release carries an end timestamp and charge splitting normalizes UTC+8 boundaries. | Static source review; no journal or boundary behavior test. |
| Actual GPU count | Commit receives post-`job_set_alloc_tres` `tres_alloc_cnt`; replay replaces intent GPU count with the recorded commit value. | Source inspection and compile; no scheduler behavior test. |
| Zero/unknown actual GPU | CPU jobs are excluded at the caller. In an enabled ledger, commit zero or `UINT64_MAX` sets unhealthy and ambiguous, blocking later allocations. Disabled mode returns without changing health. | Static source review; no runtime test. |
| Missing restored jobs | Reconciliation scans every committed record against restored active jobs and fails closed on missing evidence. | Source inspection; no restart/recovery test. |
| Durable initialization | Missing journal is not treated as empty. First creation uses an `O_EXCL` journal plus durable `ledger.initialized` marker; a later missing journal fails closed. Journal, marker, and clock-fence directory entries are fsynced. Existing data is preserved. | Static source review; no crash or filesystem fault test. |
| Clock replay and regressions | Replayed WAL timestamps are compared with current time, missing fence recovery is constrained by valid replay, and realtime/monotonic samples are paired under one mutex. | Static source review; no clock manipulation test. |
| Recovery ordering and API readiness | Reconciliation is after restored jobs and after `assoc_mgr_init`; GPU TRES absence marks the ledger ambiguous. The socket starts only after successful reconciliation, never during initialization. | Source order review; production TRES mapping unverified. |
| Admission linearization | Not claimed fixed. The real Slurm19 flow has no proven atomic quota/resource commit and complete rollback across all paths. Explicit activation is refused with `MQ_STRICT_INTEGRATION_PROVEN 0`. | Residual blocker; no strict behavior claim. |
| Scheduler I/O | Not claimed bounded. Synchronous WAL/fence/checkpoint operations remain documented as a blocker; persistence was not removed. | Residual blocker; no latency or crash test. |
| Priority truncation | Supported base priorities retain 30 low bits; larger values fail closed. | Static source review; site priority configuration unverified. |
| Destructive build output | Build helper requires a new output directory and contains no recursive removal. | Shell inspection and syntax check. |

The 26.05.5 comparison remains in the separate pro package. This 19 adapter
has no 26-only `het_job_id` or `src/stepmgr/stepmgr.h` assumptions.
