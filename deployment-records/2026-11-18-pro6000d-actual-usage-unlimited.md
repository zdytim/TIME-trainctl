# pro6000d：删除单次运行时限

- 功能提交：`09cd9d118c0c8e01faee55b9196fd8104c0b31f0`，目标分支 `pro6000d-slurm26`，已推送。
- 切换工具提交：`86bac7b`；围栏内收尾工具提交：`1cb3150`，均已推送。
- 目标：pro6000d（系统主机名 `ubuntu`）；完成时间：2026-11-18 18:04:57，Asia/Shanghai（10:04:57 UTC）。
- 组件：Slurm 26.05.5 控制器及节点、月度额度后台、Cockpit 训练管理、共享 CLI/MCP、GPU 分区与 `long` QoS。新版本路径：`/opt/slurm/26.05.5-actual-usage-20261118`。
- 原配置、服务文件、数据库快照和账本证据：`/var/backups/pro6000d-training/actual-usage-20261118T095414Z`。原 Slurm 版本保留；现存账本没有被覆盖或回滚。
- 切换经过：停止派发、确认 Slurm 队列和 GPU 计算进程为空、GPU 分区设为 INACTIVE，再停止空闲调度器。首次切换时，归档目录的组写权限导致共享客户端升级器拒绝执行；系统保持围栏且未回滚账本。收紧暂存目录权限，恢复新控制器和后台，升级共享客户端，再次核对空闲，完成分区持久配置与服务重启。原失败及恢复证据在备份目录 `release.json`。
- 配置验证：GPU 分区 `DefaultTime=UNLIMITED`、`MaxTime=UNLIMITED`、`State=UP`；`long` QoS 的 `MaxWall` 无上限；slurmctld、slurmd、pro6000d-training 均 active。控制器 `ACTUAL_USAGE_UNLIMITED2` 报告 SUPPORTED，release attestation、月度强制门控、档位与严格优先级 API 健康检查通过。
- 必要检查：后台针对性 12 项、CLI/MCP 10 项、客户端升级器 8 项、月度面板检查通过；控制器源码在目标 x86_64 构建成功。上线后 `timectl validate` 返回 policy 2 无限时间；单 GPU 冒烟任务 Slurm `88` 已 `COMPLETED`，调度器无活动任务，月度账本可读取且活动 GPU 数为 0。没有对余额耗尽、真实故障重启和重排进行生产端到端测试。
- 首发限制：新任务以 `--no-requeue` 提交，不提供自动重排；旧任务沿用原规则。旧版客户端需更新；共享安装已同步更新。
- 回退：未回滚。新格式账本一旦写入，不能使用不识别该格式的旧控制器，也不能覆盖现有 WAL；出现问题应隔离新分配，使用兼容新账本的控制器向前修复。
