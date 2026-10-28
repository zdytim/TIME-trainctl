# u20 Slurm 19 释放证据与故障关闭策略（审查草案）

范围：`u20-controller-attempt14` 及 `controller-19-release-review/normal-epilog-only.patch`，对照官方 Slurm 19 commit `78e9cfab783aeed0da68ea25532a3f35114d4072`。仅源码静态阅读；没有运行测试、训练、修改服务或启用规则。

## 不应把哪些 Slurm 状态作为 GPU 已释放的证明

| 路径/事件 | 已确认的 Slurm 语义 | 最保守月账处理 |
|---|---|---|
| `deallocate_nodes` 的 `agent_args->node_count - down_node_cnt == 0`（node_scheduler.c:593–626） | 对 DOWN/POWER_UP 节点不验证 KILL RPC，仍调用 cleanup_completing。attempt14 在这里记录 RELEASE。 | 删除/替换该错误释放路径；记录 UNKNOWN，保留原 allocation 与卡数，阻止受影响用户新分配。仅 node DOWN 不证明任务已经退出。 |
| `re_kill_job` 的 DOWN 节点（node_scheduler.c:4800–4845） | Slurm 视为完成并可运行 batch_requeue_fini；attempt14 两处分支记录 RELEASE。 | 同上，不以 Slurm 的逻辑完成替代设备核验；在 requeue 修改 submit_time 之前冻结旧 generation 的账本状态。 |
| `job_epilog_complete`（job_mgr.c:15143–15252） | 正常单节点回执由 make_node_idle 清理 COMPLETING，随后可 requeue。 | 成功回执是有用的 Slurm 终止证据，但不足以单独证明容器和 GPU 物理清除，不直接记 RELEASE。必须结合节点侧可信证据及对应的 generation。 |
| slurmd 无活动 step / 无 epilog fast path（slurmd/req.c:5470–5509） | 先发送 `ESLURMD_KILL_JOB_ALREADY_COMPLETE` 或 `_epilog_complete`，随后才执行 `container_g_delete`；delete失败只记日志。 | 回执到达时 container 清理甚至可能未执行；不能将回执当物理释放。 |
| 常规 slurmd 路径（req.c:5527–5586） | 等待步骤后运行 epilog；`container_g_delete` 即使失败仍只记日志，随后用 epilog rc 发完成消息。 | epilog rc=0 只能证明 epilog 成功，不足以证明 cgroup/container 已清除；等待可信节点证据。 |
| 异步 EpilogSlurmctld（job_scheduler.c:3921–3932） | epilog 结束且 node_cnt==0 才清掉 COMPLETING，但 node_cnt 可因 DOWN 而变0。 | `node_cnt==0` 与 `!epilog_running` 不是清理证据。冻结或关联此前确证的逐节点记录，不能自行补 RELEASE。 |
| PREPARE/START_INTENT 但 START_COMMIT 缺失 | 可能是分配过程中崩溃或持久写入失败；attempt14 `_finish(RELEASE)` 对 uncommitted 直接写 ABORT。 | UNKNOWN，不删除、不写 ABORT、不退款；重新核对 controller 资源状态、节点 job/cgroup/GPU 实情。仅可证明从未提交 GRES 时才 durable ABORT。 |
| job requeue（job_mgr.c:15257–15305） | 在最后回调后重置 start/end，部分情况改变 submit_time。 | 旧 allocation 与新尝试必须有稳定 generation；UNKNOWN 与后续 generation 不能相互覆盖。仅旧 generation 获得可靠释放证据后才结算，重新分配必须检查当前余额。 |

`normal-epilog-only.patch` 不满足上述物理释放证据要求，**不得启用/部署**。attempt14 的三个 `monthly_quota_release` 调用（patch:108、176、184）必须移除或替换为可信 `UNKNOWN` 事件；不能只追加 normal hook。

## 建议的 fail-closed 状态模型

1. 独立于 Slurm 的 RUNNING/COMPLETING，按 cluster + uid + job_id + submit identity + restart/allocation generation + node 记录 `INTENT / COMMITTED / RELEASE_UNCERTAIN / RELEASE_VERIFIED / ABORT_VERIFIED`。启动前状态明确且必须持久；generation 不使用单独 PID 或 jobid。
2. DOWN、回执无法证明清除、未知 commit、epilog 异步、磁盘故障等首次观察时写入幂等 `RELEASE_UNCERTAIN` 和原因，**不移除 allocation、不生成 RELEASE、不伪造结束时间**。记录失败或状态丢失时保守阻止所有无法安全归属的 GPU 新分配。
3. `RELEASE_UNCERTAIN` 期间对用户可展示“用量未确认/停止新启动”，不可展示为正在训练或持续精确计费；即使账面假设 GPU 持有也不得把估值当精确最终账单。原有运行任务不因账户冻结被杀。用户私有数据、节点安全及其他用户公平由实际场景和权限决定。
4. 可信节点侧证明必须约定：已认证节点身份和启动世代（boot ID）、allocation generation、作业 cgroup/容器空及清理完成、GPU 上目标作业关联进程已退出、时间来源及签名/完整性；记录所有设备和误差。仅 NVML 空进程无法单独证明 OS cgroup/孤儿均已清除，节点不可达或证据互相矛盾保持 UNKNOWN。此段为设计目标，尚无节点代理、远程身份与证据协议。
5. 串行验证消息身份、generation、顺序和 idempotency；然后原子持久 `RELEASE_VERIFIED`（准确时长依据可接受证据，不可由恢复时钟硬猜）。真实结束时刻不明则挂起计费，由管理员基于明确证据审计修复；不能凭“已重启”推算前一月精确用量。
6. 后台重启先恢复 WAL 和 per-node 未决记录，再核对 slurmctld + slurmd；完毕以前禁止新 GPU 分配。重试/重排队是新 generation，旧未决状态不能自动继承为成功释放。release/abort/grant 均需要事务化幂等并验证同 ID 同 payload。
7. 管理员恢复工具先展示证据、影响、未知区间；只有授权后附原因、证据摘要、操作者和幂等 ID 追加审计事件，禁止直接覆盖 WAL/清零。故障不能自动被视作 0 用量。

## 不能误解的边界

- 用户要求 normal 耗尽后已有任务不中断、之后任务不分配 GPU。未知时按受影响用户冻结是更保守的处理，不能误用此策略中止已运行任务。
- 上述方法引入节点侧协议和重启恢复机制，当前代码没有实现。无法仅通过给 `job_epilog_complete`、`cleanup_completing` 加 hook 满足严格物理释放条件。
- 物理清理完成到节点证据到达有延迟，计费的结束时刻与证据时间并非同一事实，需要可信时钟和明确精度；不能把取证时刻伪装成精确结束时间。
- `select/cons_tres` 无操作的 `select_p_job_begin` 只说明该插件某函数当前为空，不能推导全部失败/插件及重试路径都未分配。未明确证明所有副作用与回滚前，START_INTENT 的 ABORT 同样要保守。

## 下一步

先形成独立、默认关闭的 UNKNOWN 状态/WAL schema 与节点证据协议，审查完整接入/回退，再经过用户授权的安全验证。不要运行原 install-controller.sh、不要修改 Slurm 服务，不能声称已实现或验证此设计。
