# Monthly grant idempotency candidate

The `41aaabf` candidate indexed grants by user, month and grant ID, but a repeated ID returned success without comparing amount or approving actor. A conflicting retry could therefore be reported as successful even though the requested amount was not applied. `grant-idempotency-fix.patch` stores the original amount and actor in the in-memory index, reconstructs them during WAL replay, returns success only for an identical retry and rejects conflicting reuse. It applies on top of `6725de1` plus `41aaabf`.

This is an isolated source candidate only. It does not provide an authenticated administrative grant transport, guarantee WAL tail integrity or implement priority-tier scheduling. The monthly gate remains disabled. No tests, simulations, deployment or job operations were performed.

The committed patch `b9f9f9b` was applied to the existing u20 isolated candidate after `41aaabf`; `gcc -std=gnu99 -I. -fsyntax-only src/slurmctld/monthly_quota.c` returned zero using the inherited review headers. This is a syntax check, not linking or behavioral verification, and does not prove target ABI compatibility.
