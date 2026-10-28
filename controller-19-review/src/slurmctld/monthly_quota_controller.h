#ifndef _SLURM_MONTHLY_QUOTA_CONTROLLER_H
#define _SLURM_MONTHLY_QUOTA_CONTROLLER_H

#include <stdbool.h>
#include <stdint.h>

struct job_record;

/* The controller owns this state.  A preview never creates a permit. */
int monthly_quota_init(void);
int monthly_quota_reconcile(void);
/* UINT64_MAX means the GPU TRES position is unknown; callers fail closed. */
uint64_t monthly_quota_gpu_count(uint64_t *tres_req_cnt);
void monthly_quota_fini(void);
int monthly_quota_preview(struct job_record *job_ptr, uint64_t gpu_count);
int monthly_quota_admit(struct job_record *job_ptr, uint64_t gpu_count);
void monthly_quota_commit(struct job_record *job_ptr, uint64_t gpu_count);
void monthly_quota_abort(struct job_record *job_ptr);
void monthly_quota_release(struct job_record *job_ptr);
uint32_t monthly_quota_priority(struct job_record *job_ptr, uint32_t base_priority);
bool monthly_quota_pending(struct job_record *job_ptr);

#endif
