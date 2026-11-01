/* Candidate arithmetic only: this is not a scheduler or an online ledger. */
#include "selectable-tier-accounting.h"
#include <stddef.h>
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
