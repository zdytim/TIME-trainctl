# Staged uncommitted release guard

The candidate replay handler's RELEASE condition included `strcmp(event, "RELEASE")` inside the RELEASE branch, so it could not reject a RELEASE for an uncommitted PREPARE. That could silently turn an ambiguous allocation into settled usage and remove the active record instead of stopping recovery. The candidate now requires a committed allocation in replay. The live release path also marks the ledger unhealthy and preserves the active record if it encounters an uncommitted allocation, rather than writing a RELEASE that recovery would reject.

This is fail-closed; it does not establish whether an ambiguous job actually acquired a GPU and does not repair such a ledger. Existing jobs are not terminated. Source review and candidate compilation do not replace replay, crash or behavioral validation. No test or live operation was performed.

On u20, the patch from commit `22a7c1d` applied to the existing isolated candidate and the controller recompiled successfully. The ELF SHA-256 is `aed60e8709a11e32bcc877025a6f760f3d6883bd4172e5f7a310c3bf98a73bef`; existing Slurm and training services remained active in the subsequent read-only check. The candidate was not installed or executed.
