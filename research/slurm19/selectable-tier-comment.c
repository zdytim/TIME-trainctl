/* Parsing alone does not authenticate the comment or enable scheduling. */
#include "selectable-tier-comment.h"
#include <stddef.h>
#include <string.h>

int time_parse_job_marker(const char *comment, char job_id[33], enum time_tier *tier)
{
    static const char prefix[] = "pro6000d-job:";
    const char *cursor;
    enum time_tier parsed;
    size_t i;
    if (!comment || !job_id || !tier || strncmp(comment, prefix, sizeof(prefix) - 1))
        return -1;
    cursor = comment + sizeof(prefix) - 1;
    for (i = 0; i < 32; i++) {
        char c = cursor[i];
        if (!c || !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')))
            return -1;
    }
    cursor += 32;
    if (!*cursor) {
        memcpy(job_id, cursor - 32, 32);
        job_id[32] = '\0';
        return 1;
    }
    if (!strcmp(cursor, ":tier=debug")) parsed = TIME_DEBUG;
    else if (!strcmp(cursor, ":tier=important")) parsed = TIME_IMPORTANT;
    else if (!strcmp(cursor, ":tier=normal")) parsed = TIME_NORMAL;
    else return -1;
    memcpy(job_id, cursor - 32, 32);
    job_id[32] = '\0';
    *tier = parsed;
    return 0;
}
