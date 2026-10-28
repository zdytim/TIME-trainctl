# Continue u20 controller after build review

Your attempt7 produced correct patch application and isolated compile. Independent review of the related 19 implementation raised additional production blockers, verify against exact attempt7 source and repair where still present:
1. START_COMMIT replay must update GPU count to actual recorded value, not retain intent count; writer/replay field count symmetry and cross-month split must be correct.
2. A normal non-GPU job or disabled monthly feature must not call commit(gpu=0) and set global mq_healthy=false.
3. Concurrent _now_ns must sample realtime and monotonic while holding clock lock to avoid out-of-order false regressions.
4. Disabled initialization must not require/create/fail on unavailable monthly state dir; explicit enable must fail closed if marker or state missing, and installer must make enablement explicit and safe.
5. Synchronous fsync of clock.fence from every queue priority and speculative scheduling preview holds scheduler locks and can stall scheduler; do not simply remove persistence because it weakens restart/clock regression guarantee. Design separate non-persistent preview versus allocation-commit durable WAL timestamp and recover/reconcile semantics. If you cannot prove safe, keep feature disabled and report blocked. No runtime tests/simulation, deployments or service mutation; project requires explicit test request.
6. Actual u20 configure options/dependencies differ from isolated build (munge/PAM absent); compilation alone cannot be deployed. Validate source/build specifics and provide exact build prerequisites/rollback.

Preserve other tasks and credentials; commit/deployment handled by supervisor. Put result in sibling outputs. Do not assert production-ready without behavior verification.
