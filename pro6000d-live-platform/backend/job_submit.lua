-- PRO6000D_ROOT_BROKER_V1
-- Gate every partition including an omitted/default partition.
-- Real submit_uid is authenticated by Slurm/Munge, never browser JSON.
function slurm_job_submit(job_desc, part_list, submit_uid)
    if submit_uid ~= 0 then
        slurm.log_user("Please submit through the Cockpit training platform")
        return slurm.ERROR
    end
    local id = (job_desc.comment or ""):match("^pro6000d%-job:([0-9a-f]+)$")
    if not id or #id ~= 32 then
        slurm.log_user("Missing platform submission identity")
        return slurm.ERROR
    end
    if not job_desc.user_id or job_desc.user_id == 0 or job_desc.user_id == slurm.NO_VAL then
        slurm.log_user("Training jobs must run as a non-root Unix account")
        return slurm.ERROR
    end
    return slurm.SUCCESS
end
function slurm_job_modify(job_desc, job_rec, part_list, modify_uid)
    if modify_uid ~= 0 then
        slurm.log_user("Resource changes must be approved through the training platform")
        return slurm.ERROR
    end
    return slurm.SUCCESS
end
return slurm.SUCCESS
