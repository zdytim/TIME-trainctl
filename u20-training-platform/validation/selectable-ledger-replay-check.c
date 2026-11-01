/* Isolated in-memory replay check. Compile against the reconstructed source;
 * never connect to a running controller or load its WAL. */
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "src/slurmctld/monthly_quota.c"

#define HOUR (UINT64_C(3600) * UINT64_C(1000000000))

int main(void)
{
    uint32_t month;
    const mq_time_t start = { .sec = 1735689600, .nsec = 0 };
    mq_month_record_t *account;
    mq_active_record_t *first;
    char *cutover[] = {"U20MQ1", "1", "CUTOVER2_RESET_USAGE"};
    char *prepare1[] = {"U20MQ1", "2", "PREPARE2", "10", "1001", "1", "4", "1735689600", "0", "1", "0123456789abcdef0123456789abcdef"};
    char *commit1[] = {"U20MQ1", "3", "COMMIT", "10", "1"};
    char *advance1[] = {"U20MQ1", "4", "ADVANCE2", "1735696800", "0"};
    char *badid[] = {"U20MQ1", "5", "PREPARE2", "11", "1001", "2", "1", "1735696800", "0", "0", "invalid"};
    char *prepare2[] = {"U20MQ1", "5", "PREPARE2", "11", "1001", "2", "1", "1735696800", "0", "0", "fedcba9876543210fedcba9876543210"};
    char *commit2[] = {"U20MQ1", "6", "COMMIT", "11", "2"};
    char *advance2[] = {"U20MQ1", "7", "ADVANCE2", "1735718400", "0"};
    char *release1[] = {"U20MQ1", "8", "RELEASE2", "10", "1", "1735718400", "0"};
    char *release2[] = {"U20MQ1", "9", "RELEASE2", "11", "2", "1735718400", "0"};
    char month_text[16];
    char *grant[] = {"U20MQ1", "10", "GRANT2", "1001", month_text, "3600", "0", "grant-1", "admin", "1735718400", "0"};

    mq_next_generation = 1;
    assert(_month_key(&start, &month) == 0);
    snprintf(month_text, sizeof(month_text), "%u", month);
    account = _find_month(1001, month, true);
    account->usage.sec = 6 * 3600; /* legacy usage must not be imported */
    assert(_replay_event(cutover, 3) == 0);
    assert(account->selectable.used[TIME_DEBUG] == 0);
    assert(_replay_event(prepare1, 11) == 0);
    first = _find_active(10, 1);
    assert(first && strcmp(first->marker_id, prepare1[10]) == 0);
    assert(_replay_event(commit1, 5) == 0);
    assert(_replay_event(advance1, 5) == 0);
    assert(account->selectable.used[TIME_DEBUG] == 0);
    assert(account->selectable.used[TIME_IMPORTANT] == 8 * HOUR);
    assert(_replay_event(badid, 11) == -1);
    assert(_find_active_job(11) == NULL);
    assert(_replay_event(prepare2, 11) == 0);
    assert(_replay_event(commit2, 5) == 0);
    assert(_replay_event(advance2, 5) == 0);
    assert(account->selectable.used[TIME_DEBUG] == 5 * HOUR);
    assert(account->selectable.used[TIME_IMPORTANT] == 33 * HOUR);
    assert(account->selectable.used[TIME_NORMAL] == 0);
    assert(_replay_event(release1, 7) == 0);
    assert(_replay_event(release2, 7) == 0);
    assert(mq_active == NULL);
    assert(_replay_event(grant, 11) == 0);
    assert(account->selectable.limit[TIME_NORMAL] == 501 * HOUR);
    assert(_replay_event(grant, 11) == -1); /* duplicate grant record */
    _free_memory();
    puts("PASS: isolated selectable ledger replay, identity, concurrent 4-GPU charging, rollover and grant");
    return 0;
}
