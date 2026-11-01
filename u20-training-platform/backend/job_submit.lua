-- PRO6000D_ROOT_BROKER_V1
-- Gate every partition including an omitted/default partition.
-- Real submit_uid is authenticated by Slurm/Munge, never browser JSON.
-- Keep selected-tier markers disabled until the matching controller has been
-- installed, verified and its durable per-tier accounting activated.
local selectable_tier_enforcement = false
function slurm_job_submit(job_desc, part_list, submit_uid)
    if submit_uid ~= 0 then
        slurm.log_user("Please submit through the Cockpit training platform")
        return slurm.ERROR
    end
    -- The optional tier is authored by the root broker, never by the user.
    -- Deploy this parser together with a controller that enforces the tier;
    -- accepting the marker alone does not implement quota accounting.
    local comment = job_desc.comment or ""
    local id, tier = comment:match("^pro6000d%-job:([0-9a-f]+):tier=([a-z]+)$")
    if not id then
        if selectable_tier_enforcement then
            slurm.log_user("A platform quota tier is required")
            return slurm.ERROR
        end
        id = comment:match("^pro6000d%-job:([0-9a-f]+)$")
    elseif tier ~= "debug" and tier ~= "important" and tier ~= "normal" then
        slurm.log_user("Invalid platform quota tier")
        return slurm.ERROR
    elseif not selectable_tier_enforcement then
        slurm.log_user("Selected quota tiers are not yet enforced")
        return slurm.ERROR
    end
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
