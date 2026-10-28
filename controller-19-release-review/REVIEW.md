# Slurm 19 正常 epilog 释放记账：仅供审查的窄范围补丁

## 结果和状态

`normal-epilog-only.patch` 是应用于 `u20-controller-attempt14` 已打到官方 Slurm 19.05.5 源码后的附加补丁。它只修改 `job_mgr.c` 的普通 node epilog 回调；没有变更其它规划器问题，也没有启用或部署。

仅在成功收到唯一一台非 DOWN 节点的 epilog 完成、节点仍在 completing bitmap、node_cnt=1、return_code=0、没有仍在运行的控制器 epilog 时，`make_node_idle` 确认退出 COMPLETING 后，在 `batch_requeue_fini` 修改 submit_time 之前调用月账 release。重复回调先被原有非运行状态/bitmap 检查拒绝；已释放的 controller allocation `_find_alloc` 为空时再次 release 为 no-op。这些是代码静态推断，未经运行验证。

## 为什么仍不能启用

- `epilog complete` 是 Slurm 的节点回执，不是独立的 GPU 物理进程/cgroup 核验；当前不能证明其它进程、错误驱动及节点断链下的物理释放。
- 多节点、DOWN、无有效 bitmap、epilog 返回错误、仍有 EpilogSlurmctld 的路径故意不关闭账本，必须能将不明状态维持为 fail-closed 并补充恢复策略。未定义新持久事件，不可宣称这些路径已完整处理。
- 原 attempt14 仍保留节点 down 的 `deallocate_nodes` release 和 `re_kill_job` 其他 hook，节点失联可能不能证明物理 GPU 已释放；本次依约未修改这些路径。若启用，仍可能提前停账。
- `_finish` 遇未提交的 allocation 会写 ABORT；若出现真实资源已分配但 START_COMMIT 失败，这会丢失计费证据。必须修复并验证恢复/提交事务，不能靠本补丁保证安全。
- 若 `make_node_idle` 因意外提前清除 COMPLETING，需确认该状态转移与仅一节点计数和所有相关步骤释放的一致性；包括 slurmd 重启、重复回执、job ID/submit 身份复用和 requeue。账本引用的是 `(uid, jobid, submit_time)`，非正式定义的全局 allocation generation。
- 补丁没有解决 admission 与真实 GRES 分配时序、财务账本丢失、时钟恢复、设备份额，以及 u20 运行环境的适配和备份。

## 静态证据

- 官方源 commit `78e9cfab783aeed0da68ea25532a3f35114d4072`。
- `job_mgr.c:15143–15252` 最终回执；`node_mgr.c:3935–3972` 在节点完成后清理标志；`job_scheduler.c:3921–3931` 控制器 epilog 可晚于节点回执，`cleanup_completing` 在 `job_scheduler.c:4884`。
- 正常及失联路径同时会进入 `cleanup_completing`，因此不能把其函数入口一律当成物理释放证据。
- 先对独立官方源码副本应用 attempt14，再执行 `git apply --check normal-epilog-only.patch`，返回成功；补丁格式检查通过。未运行测试、调度、构建产物、部署或服务重启。
