/* Candidate arithmetic for selectable quota tiers; not wired into Slurm. */
#ifndef TIME_SELECTABLE_TIER_ACCOUNTING_H
#define TIME_SELECTABLE_TIER_ACCOUNTING_H
#include <stdint.h>

#define TIME_TIER_COUNT 3

enum time_tier { TIME_DEBUG = 0, TIME_IMPORTANT = 1, TIME_NORMAL = 2 };

/* Values are integer GPU-nanoseconds, including approved additions in limit.
 * Callers own persistence, synchronization and calendar boundaries. */
struct time_tier_balance {
    uint64_t limit[TIME_TIER_COUNT];
    uint64_t used[TIME_TIER_COUNT];
    uint64_t overage;
};

/* A new job may start only in its explicitly chosen, nonempty tier. */
int time_tier_eligible(const struct time_tier_balance *balance, enum time_tier selected);

/* Charge a running allocation, overflowing toward lower priorities only.
 * Updates the task cursor to the next nonempty tier. Returns -1 on malformed
 * state/overflow, with *balance and *current unchanged. */
int time_tier_charge(struct time_tier_balance *balance, enum time_tier *current,
                     uint64_t gpu_nanoseconds);

/* A normal-tier grant first offsets overage. Returns -1 without mutation on
 * malformed state/overflow; durable idempotency is the caller's obligation. */
int time_tier_add_normal(struct time_tier_balance *balance, uint64_t grant);
/* Candidate boundary calculation for one user's concurrently running jobs.
 * Rates are allocated GPU counts; current tiers represent running jobs, not
 * new submissions. Returns the earliest wall-nanosecond where a shared tier
 * can be depleted, capped by horizon. Does not mutate balances or jobs.
 * The boundary can split GPU-nanoseconds and callers must assign the remainder
 * deterministically before moving jobs; this helper does not perform charging. */
struct time_running_allocation {
    enum time_tier current;
    uint64_t gpu_count;
};
int time_tier_next_boundary(const struct time_tier_balance *balance,
                            const struct time_running_allocation *jobs,
                            unsigned count, uint64_t horizon_ns,
                            uint64_t *boundary_ns, int *depletes);

/* Import a verified, settled legacy monthly total and grants using the old
 * debug -> important -> normal ordering. Caller must prove a consistent
 * controller snapshot, preserve the original ledger, and prevent live writes.
 * On failure *out is unchanged; this function does not migrate a database. */
int time_tier_import_legacy(struct time_tier_balance *out,
                            uint64_t used_gpu_nanoseconds,
                            uint64_t grant_gpu_nanoseconds);
#endif
