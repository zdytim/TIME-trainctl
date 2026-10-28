/* Root broker admission policy for the installed Slurm 26.05 ABI.
 * Existing jobs/steps are not stopped or modified by this plugin.
 */
#include <stdint.h>
#include <string.h>
#include <ctype.h>
#include <slurm/slurm.h>
#include <slurm/slurm_version.h>
#include <slurm/slurm_errno.h>

const char plugin_name[] = "Pro6000d platform root broker admission";
const char plugin_type[] = "job_submit/pro6000d";
const uint32_t plugin_version = SLURM_VERSION_NUMBER;

int init(void) { return SLURM_SUCCESS; }
void fini(void) { }

int job_submit(job_desc_msg_t *job, uint32_t uid, char **error_msg)
{
    static const char prefix[] = "pro6000d-job:";
    (void)error_msg;
    if (uid != 0 || !job || job->user_id == 0 || job->user_id == NO_VAL || !job->comment)
        return ESLURM_ACCESS_DENIED;
    if (strncmp(job->comment, prefix, sizeof(prefix)-1) != 0 ||
        strlen(job->comment) != sizeof(prefix)-1+32)
        return ESLURM_ACCESS_DENIED;
    for (const char *p=job->comment+sizeof(prefix)-1; *p; ++p)
        if (!isxdigit((unsigned char)*p)) return ESLURM_ACCESS_DENIED;
    return SLURM_SUCCESS;
}

/* Slurm 26.05 added error_msg to the job_modify callback. */
int job_modify(job_desc_msg_t *job, void *job_record, uint32_t uid, char **error_msg)
{
    (void)job; (void)job_record; (void)error_msg;
    return uid == 0 ? SLURM_SUCCESS : ESLURM_ACCESS_DENIED;
}
