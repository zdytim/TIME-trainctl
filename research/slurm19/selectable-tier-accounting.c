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
