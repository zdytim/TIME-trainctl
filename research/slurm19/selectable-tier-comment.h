/* Candidate parser for broker-authored Slurm job identities. */
#ifndef TIME_SELECTABLE_TIER_COMMENT_H
#define TIME_SELECTABLE_TIER_COMMENT_H
#include "selectable-tier-accounting.h"
/* Returns 0 for a selected tier, 1 for a valid legacy marker with no tier,
 * and -1 for an invalid marker. On failure, outputs remain unchanged.
 * The marker is trusted only when enforced by the root-only submit plugin. */
int time_parse_job_marker(const char *comment, char job_id[33], enum time_tier *tier);
#endif
