# Targeted source evidence

- `src/slurmctld/node_scheduler.c:2862-2883`: `_preempt_jobs` runs before the current quota gate.
- `src/slurmctld/node_scheduler.c:2960-2975`: current preview/admission point.
- `src/slurmctld/node_scheduler.c:3002-3034`: burst-buffer begin and select begin.
- `src/slurmctld/node_scheduler.c:3085-3105`: select nodeinfo, then node allocation.
- `src/slurmctld/node_scheduler.c:3120-3124`: actual allocated TRES materialization and current commit point.
- `src/plugins/select/cons_tres/select_cons_tres.c:189-237`: `_add_job_to_res` mutates select state and calls `gres_plugin_job_alloc`.
- `src/common/gres.c:10364-10424`: GRES allocation iterates plugin contexts and may return an error after partial work.
- `src/plugins/select/cons_tres/select_cons_tres.c:1728-1737`: `select_p_job_fini` calls `rm_job_res`.
- `src/plugins/select/cons_tres/job_test.c:494-555`: removal calls `gres_plugin_job_dealloc` and updates select state.
- `src/slurmctld/burst_buffer.c:621-665`: begin and revoke; revoke explicitly does not release prior resources.
- `src/plugins/sched/backfill/backfill.c:2717-2758`: backfill starts jobs through `select_nodes(..., false, ...)`.
- `src/slurmctld/job_mgr.c:4738-4847`: partition wrapper can call `select_nodes` repeatedly, including preview/real variants.
- `src/slurmctld/node_scheduler.c:500-503`: select fini precedes termination RPC dispatch.
- `src/slurmctld/job_mgr.c:15143-15243`: final epilog callback and `make_node_idle`.
- `src/slurmd/slurmd/req.c:5482-5508`: completion notification can precede `container_g_delete`, whose failure is only logged.
- `src/common/node_select.c:66-90, 547-559, 637-648`: select plugin ABI has begin/fini but no transaction prepare/commit/rollback.
