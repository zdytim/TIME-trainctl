# u20 月度控制器第一阶段切换

- 2026-10-31 15:24 CST；目标 u20 RTX 4090；部署切换器 Git `79a1936`、候选 Slurm 构建 `7a06cff`，分支 `u20-slurm19`。原平台后端与页面代码未替换，月度页面仍处于 staged，尚未对用户宣称验证完成。
- 空队列与版本预检后，进入维护并停止后端与原控制器，创建快照 `/var/backups/pro6000d-training/cutover-stopped-p92s3veb`；离线事务清理 7 条已完成平台任务，保留 7 条请求身份 tombstone，清空与这些任务匹配的 7 条 jobcomp 记录。其他表由受限迁移逻辑保护；Slurm 内部旧完成作业仍由其正常保留策略管理，未删除整个 Slurm 状态目录或重置编号。
- 创建新空账本及 root 见证文件，安装已预置 unit 并在 systemd 下启动候选控制器；控制接口报告 timzhang 为 debug、零用量、605 GPU·小时总量、零活动分配。开放后端前写入切换阶段记录。第一阶段 transient unit `time-monthly-activation-20261031-1525.service` 返回成功。
- 独立复查：slurmctld、slurmd、训练后端 active；控制器可执行文件为 `/opt/time/slurm19-7a06cfff70a7/sbin/slurmctld`，`SlurmctldParameters=MonthlyGpuQuota`；平台任务数 0、tombstone 7、jobcomp 大小 0、调度队列空，维护标记不存在，GPU 设备 other ACL 为 `---`。
- 本次没有回滚。维护前快照及迁移日志保留于上述 root 私有目录；一旦重新开放提交，不能盲目恢复旧数据库或账本，必须先核实当前作业及新增数据并采用已审阅的恢复流程。
- **状态限制：** 控制器进程已加载月度参数并返回初始账本，但本次尚未在生产窗口验证真实 PyTorch 训练计量、额度耗尽时保持排队及已运行任务超额继续。UI 和额度申请功能仍未启用；在这些核验及前端切换前不得宣称功能已完成上线。

后续执行已提交 GPU 探针 `4a26479`（当前脚本 SHA-256 `935f7c2deea809fb4e010ebffdee975033fe8d2ecee6436b0c1747c11a6cbaac`），以 timzhang 通过平台提交真实 Slurm 任务 9，PyTorch 在 RTX 4090 上计算成功并输出 `TIME_GPU_TORCH_OK`。任务结束后控制器对该账户报告 debug、已用 0.981343221 GPU·秒、零活动分配、WAL sequence 3；队列空、三个服务 active、GPU other ACL 仍为 `---`。这是一次短作业的实际计量，不覆盖生产额度耗尽或跨档、持续超额运行、多人并发和断电恢复；月度 UI 和申请功能仍为 staged，故整套功能尚未完成上线。
