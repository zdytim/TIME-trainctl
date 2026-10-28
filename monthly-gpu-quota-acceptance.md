# 月度 GPU·小时额度：实施约束

用户确认：每用户每设备每自然月（北京时间）debug 5 → important 100 → normal 500 GPU·小时。按分配卡数×实际占用时长，以GPU秒持久化；排队不扣。normal耗尽保留所有已运行任务至结束，超额继续计账；新任务及原PENDING不得启动。管理员批准追加normal额度，先抵扣既有超额，余额为正才可启动。

## 当前实机前置情况（2026-10-28）
- pro6000d：Slurm26.05.5，slurmdbd，AccountingStorageEnforce=associations,limits,qos，priority/multifactor，PriorityWeightQOS=0，native job_submit/pro6000d。
- u20：Slurm19.05.5，accounting_storage/none，priority/basic，Lua准入，无Prolog；需补齐可靠历史计费/门控才可启用月额。
- 查询时两机无活动Slurm作业。部署前仍需重新查询。
- 两机仍独立账本，Cockpit主机切换不会合并余额。

## 必须评审的场景（不是已运行的测试）
1. 四卡两小时：debug扣5，important扣3，总8 GPU小时。
2. 两任务并行：按每个实际分配的GPU秒合计；周期重叠不能漏记或重复扣。
3. normal零余额：已运行任务继续；已在Slurm排队任务也不能启动。
4. 运行超额8小时，批准追加5仍超额3不能启动，追加10则余额2（同时扣除此期间继续产生的用量）。
5. 23:59跨月：按北京时间月份拆分，不能重置总计或将前月尾款扣到下月。
6. API重启期间：可靠补账；证据不全则停止新的启动，不能按零用量放行。
7. 取消/失败/requeue：原已用量不退款；requeue是新一轮启动，不继承已运行豁免绕过额度。
8. 部署前任务：从启用切换时间后记账，保留运行；任务id复用不能关联错人。
9. 优先级：后端账本与Slurm排队实际优先级对应，不只改变UI字符串。
10. 没有Monthly功能时旧API/客户端/现有任务不被意外改写；升级回退保留账本与审计。

不要采用会杀运行任务的GrpTRESMins硬额度，也不采用必须预留整个声明时限的safe方式冒充允许末尾透支。禁止仅删除PENDING额度计数而没有实际启动限制。

## 授权范围
允许实现和本任务所需部署、语法/健康检查；不新增或运行测试/训练任务，不改密码、设备驱动、FRP，不停止现有训练，不重启Slurm守护进程。
