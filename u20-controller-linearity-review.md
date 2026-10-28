# Slurm 19 资源分配和月额准入静态审查

范围：官方 `slurm-19-05-5-1` 本地源码 `/tmp/time-slurm-19-source`，对照 `u20-controller-attempt14/patches/0001-strict-monthly-quota-controller-19.05.5.patch`。只读审查；未运行测试、调度、训练或部署。

## 调度顺序与副作用

`src/slurmctld/node_scheduler.c` 的 `select_nodes`：

1. 约 2795–2805：`_get_req_features` 选择资源；内部 `select_g_job_test` 可以使用 `SELECT_MODE_RUN_NOW`。选择可能写入 job_record 的候选资源，需要进一步核对失败清理。
2. 约 2862–2876：`_preempt_jobs`，可能对其他作业执行抢占。attempt14 的月额检查放在原 2960 附近，因此可能对最终被额度拒绝的作业产生抢占副作用。
3. 原 2960：attempt14 的 admission 检查和 WAL intent。
4. 约 3002–3015：burst buffer 和 `select_g_job_begin`；19 `select/cons_tres` 的 `select_p_job_begin` 为空操作，但其他插件未证明。
5. 3060–3063：设置 JOB_RUNNING，`select_g_select_nodeinfo_set` 在 `src/plugins/select/cons_tres/select_cons_tres.c:2014–2024` 调用 `_add_job_to_res`；该函数约 229 调用 `gres_plugin_job_alloc`。这是实际 GRES 分配重要边界，发生在 `allocate_nodes` 之前。
6. 3083：`allocate_nodes` 更改节点状态并领用许可证；3099：`job_set_alloc_tres`。attempt14 在该点后才读实际 GPU 数，早于此的余额检查到实际提交之间已经经过可阻塞操作。

**待修补约束：**在允许 preemption 之前进行只读月额前检；该前检不代表最终许可。主调度、backfill、immediate 等最终路径需要在实际资源提交前统一重新核算，覆盖错误/回滚和资源选择副作用。普通不受支持路径明确 fail closed。已运行任务不可因为额度耗尽被终止。

## 线性化的未解决问题

当前 `monthly_quota_admit` 在资源提交之前检查、记录 intent、释放互斥锁；已有训练的真实消耗仍在随时间增长。进入 `_add_job_to_res` 之前余额可能变为零。单纯持有 mutex 不会冻结时间，且 WAL 的 fsync 自身可能阻塞。

可研究在确认资源形态后、cons_tres 真正提交 GRES 前设置同一锁下的最终核算点，并定义有界 deadline、持久许可、失败清理与实际卡数/恢复证据。必须回答如下问题后才能声称满足用户严格要求：最终判定的精确时间与实际 GRES 提交如何线性化；在 fsync、线程被抢占或磁盘故障跨过耗尽时如何保证不产生 GPU 分配；提交后持久化失败如何保留已有运行并阻止新任务；所有代码路径和插件副作用如何撤销。若把较早 intent 视作获准时刻，应明确这会允许物理分配在余额耗尽之后，不能冒称用户选择的严格语义。

## 普通完成时释放遗漏

官方 `job_mgr.c:15143–15252` 的正常 `job_epilog_complete` 在 `make_node_idle` 后、`!IS_JOB_COMPLETING` 时调用 `batch_requeue_fini`；`node_mgr.c:3969–3972` 在最后节点及 epilog 完成时调用 `cleanup_completing`。attempt14 的 release hook 仅位于 `node_scheduler.c` 的部分分支，没有覆盖此正常 epilog 路径。账本可能继续按运行中累计已完成任务；重排队还会在 `batch_requeue_fini` 改 submit_time，导致旧 allocation 身份丢失。

建议统一在最后节点确实释放后的完成边界记录 RELEASE，且在 requeue 更改身份前落账；还须处理节点 down、async epilog、重复确认、重启恢复。不能在 deallocate 起始阶段假定用户进程和 GPU 已释放，也不能遗漏其他 `cleanup_completing` 调用者。

## 结论

当前补丁尚不能上线；本审查仅给出具体代码证据及可研究的修补方向，没有声称设计已满足严格语义。必须进一步实现、在授权的隔离验证窗口检验并审核生产构建与回退流程。
