/* Candidate arithmetic only: this is not a scheduler or an online ledger. */
#include "selectable-tier-accounting.h"
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

static int valid(const struct time_tier_balance *balance)
{
    unsigned i;
    if (!balance) return 0;
    if (balance->overage && balance->used[TIME_NORMAL] != balance->limit[TIME_NORMAL]) return 0;
    for (i = 0; i < TIME_TIER_COUNT; i++)
        if (balance->used[i] > balance->limit[i]) return 0;
    return 1;
}

int time_tier_eligible(const struct time_tier_balance *balance, enum time_tier selected)
{
    if (!valid(balance) || (unsigned)selected >= TIME_TIER_COUNT) return -1;
    return balance->used[selected] < balance->limit[selected];
}

int time_tier_charge(struct time_tier_balance *balance, enum time_tier *current,
                     uint64_t gpu_nanoseconds)
{
    struct time_tier_balance next;
    unsigned i;
    if (!valid(balance) || !current || (unsigned)*current >= TIME_TIER_COUNT) return -1;
    next = *balance;
    i = (unsigned)*current;
    while (i < TIME_TIER_COUNT) {
        uint64_t available = next.limit[i] - next.used[i];
        uint64_t charge = gpu_nanoseconds < available ? gpu_nanoseconds : available;
        next.used[i] += charge;
        gpu_nanoseconds -= charge;
        if (next.used[i] < next.limit[i] || i == TIME_NORMAL) break;
        i++;
    }
    if (UINT64_MAX - next.overage < gpu_nanoseconds) return -1;
    next.overage += gpu_nanoseconds;
    *balance = next;
    *current = (enum time_tier)i;
    return 0;
}

int time_tier_add_normal(struct time_tier_balance *balance, uint64_t grant)
{
    struct time_tier_balance next;
    uint64_t offset;
    if (!valid(balance) || !grant || UINT64_MAX - balance->limit[TIME_NORMAL] < grant)
        return -1;
    next = *balance;
    next.limit[TIME_NORMAL] += grant;
    offset = next.overage < grant ? next.overage : grant;
    next.used[TIME_NORMAL] += offset;
    next.overage -= offset;
    *balance = next;
    return 0;
}

int time_tier_import_legacy(struct time_tier_balance *out,
                            uint64_t used_gpu_nanoseconds,
                            uint64_t grant_gpu_nanoseconds)
{
    const uint64_t ns_per_hour = UINT64_C(3600) * UINT64_C(1000000000);
    struct time_tier_balance imported = {
        .limit = {UINT64_C(5) * ns_per_hour, UINT64_C(100) * ns_per_hour,
                  UINT64_C(500) * ns_per_hour},
        .used = {0, 0, 0}, .overage = 0
    };
    enum time_tier current = TIME_DEBUG;
    if (!out || UINT64_MAX - imported.limit[TIME_NORMAL] < grant_gpu_nanoseconds)
        return -1;
    imported.limit[TIME_NORMAL] += grant_gpu_nanoseconds;
    if (time_tier_charge(&imported, &current, used_gpu_nanoseconds))
        return -1;
    *out = imported;
    return 0;
}

int time_tier_next_boundary(const struct time_tier_balance *balance,
                            const struct time_running_allocation *jobs,
                            unsigned count, uint64_t horizon_ns,
                            uint64_t *boundary_ns, int *depletes)
{
    uint64_t rates[TIME_TIER_COUNT] = {0, 0, 0};
    uint64_t earliest = horizon_ns;
    unsigned j, t;
    int found = 0;
    if (!valid(balance) || (count && !jobs) || !boundary_ns || !depletes)
        return -1;
    for (j = 0; j < count; j++) {
        unsigned tier = (unsigned)jobs[j].current;
        if (tier >= TIME_TIER_COUNT || !jobs[j].gpu_count) return -1;
        while (tier < TIME_NORMAL && balance->used[tier] == balance->limit[tier])
            tier++;
        /* Fully depleted normal use is overage and has no next tier boundary. */
        if (balance->used[tier] == balance->limit[tier]) continue;
        if (UINT64_MAX - rates[tier] < jobs[j].gpu_count) return -1;
        rates[tier] += jobs[j].gpu_count;
    }
    for (t = 0; t < TIME_TIER_COUNT; t++) {
        uint64_t remaining, delta;
        if (!rates[t]) continue;
        remaining = balance->limit[t] - balance->used[t];
        delta = remaining / rates[t] + (remaining % rates[t] != 0);
        if (delta <= earliest) {
            earliest = delta;
            found = 1;
        }
    }
    *boundary_ns = earliest;
    *depletes = found;
    return 0;
}

int time_tier_advance(struct time_tier_balance *balance,
                      struct time_active_job *jobs, unsigned count,
                      uint64_t wall_nanoseconds)
{
    struct time_tier_balance next;
    struct time_active_job *copy = NULL;
    struct time_running_allocation *rates = NULL;
    uint64_t remaining = wall_nanoseconds;
    unsigned i;
    int rc = -1;
    /* Keep controller work and temporary memory bounded. */
    if (!valid(balance) || (count && !jobs) || count > 4096) return -1;
    for (i = 0; i < count; i++) {
        if (!jobs[i].job_id || !jobs[i].generation || !jobs[i].gpu_count ||
            (unsigned)jobs[i].current >= TIME_TIER_COUNT ||
            (i && (jobs[i-1].job_id > jobs[i].job_id ||
             (jobs[i-1].job_id == jobs[i].job_id &&
              jobs[i-1].generation >= jobs[i].generation)))) return -1;
    }
    if (!count || !remaining) return 0;
    copy = malloc(count * sizeof(*copy));
    rates = malloc(count * sizeof(*rates));
    if (!copy || !rates) goto out;
    memcpy(copy, jobs, count * sizeof(*copy));
    next = *balance;
    while (remaining) {
        uint64_t delta, bulk;
        int depletes;
        for (i = 0; i < count; i++) {
            rates[i].current = copy[i].current;
            rates[i].gpu_count = copy[i].gpu_count;
        }
        if (time_tier_next_boundary(&next, rates, count, remaining, &delta, &depletes) ||
            !delta || delta > remaining) goto out;
        /* Before the boundary no tier can be exhausted. At the last wall
         * nanosecond, stable job order assigns indivisible GPU-nanoseconds. */
        bulk = depletes ? delta - 1 : delta;
        for (i = 0; i < count; i++) {
            if (bulk && copy[i].gpu_count > UINT64_MAX / bulk) goto out;
            if (time_tier_charge(&next, &copy[i].current, bulk * copy[i].gpu_count))
                goto out;
        }
        if (depletes) {
            for (i = 0; i < count; i++)
                if (time_tier_charge(&next, &copy[i].current, copy[i].gpu_count))
                    goto out;
        }
        remaining -= delta;
    }
    *balance = next;
    memcpy(jobs, copy, count * sizeof(*copy));
    rc = 0;
out:
    free(rates);
    free(copy);
    return rc;
}
