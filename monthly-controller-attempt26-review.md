# u20 controller attempt 26 independent review

Scope: agent attempt26 output `strict-monthly-quota-19.05.5-u20-attempt16`. Static review only, no runtime tests, no deployment.

Confirmed by source: candidate patch removes select plugin changes, keeps MQ_STRICT_INTEGRATION_PROVEN=0, default disabled and explicit enable rejects; checksum now uses relative artifact paths and verifies destination bytes.

Remaining blockers: UNKNOWN release currently only flags global memory health/ambiguous, no durable UNKNOWN journal or node evidence/recovery; a completed ordinary job can prevent all later GPU jobs indefinitely with no recovery path. Failure paths for burst-buffer/select still append ABORT and remove intent although resource rollback is unproven: keep durable UNKNOWN until evidence. Allocation linearization, durable I/O bounds, target build ABI/PAM/munge/TRES remain unresolved. Package contains older patch variants: use only explicit attempt16 patch and isolate obsolete variants. build-controller.sh uses `sh -c` for configure args and paths: shell metacharacters/space paths can cause unsafe interpretation; use explicit argv and working-directory handling instead. This is not an approval to deploy or enable.
