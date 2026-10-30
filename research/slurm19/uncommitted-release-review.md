# Staged uncommitted release guard

The candidate replay handler's RELEASE condition included `strcmp(event, "RELEASE")` inside the RELEASE branch, so it could not reject a RELEASE for an uncommitted PREPARE. That could silently turn an ambiguous allocation into settled usage and remove the active record instead of stopping recovery. The candidate now requires a committed allocation in replay. The live release path also marks the ledger unhealthy and preserves the active record if it encounters an uncommitted allocation, rather than writing a RELEASE that recovery would reject.

This is fail-closed; it does not establish whether an ambiguous job actually acquired a GPU and does not repair such a ledger. Existing jobs are not terminated. Source review and candidate compilation do not replace replay, crash or behavioral validation. No test or live operation was performed.
