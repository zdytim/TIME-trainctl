# u20 候选控制器实机启动及回退演练

- 2026-10-31 14:56 CST；目标 u20。使用已提交脚本 `82e8a76` 和候选 Slurm 控制器 `7a06cff`。先检查服务活跃、调度队列空、原控制器及配置符合预期；后端维护标记阻止普通平台提交。
- 在真实 systemd 下停止后端与原控制器，生成受保护快照，初始化全新账本和见证文件，临时加载候选控制器 unit。systemd 启动的进程被确认指向候选控制器，月度控制接口对 timzhang 返回 debug、零消耗、605 GPU·小时总量、无活动分配。没有在该窗口提交训练或改变旧记录。
- 随后停止候选控制器，移除临时 unit，将候选 WAL、checkpoint 及见证文件保留于 `/var/backups/pro6000d-training/cutover-stopped-ug1785e9/candidate-probe`，启动原控制器和后端并完成健康检查。演练 transient unit 报告 `Result=success`、`ExecMainStatus=0`。独立复查确认原控制器可执行文件 `/usr/sbin/slurmctld-wlm`、三个服务 active、队列空、维护标记和生产候选账本均不存在，GPU ACL 的 other 权限仍为 `---`。
- 演练前的快照在 `/var/backups/pro6000d-training/cutover-stopped-ug1785e9`；独立验证清单中 40 个文件哈希及 SQLite `quick_check`，含 7 条旧任务。它只对应本次窗口，正式切换前须重新快照。候选账本及快照不提交 Git。
- 故障路径以 `0ad5ba5` 的隔离模拟 fixture 验证，涵盖新作业、快照/账本/见证失败、候选启停、额度接口、原控制器和后端启动失败；实际进程突然中断、断电及生产恢复失败未演练。一次更早的 SSH 尝试在控制连接断开时结果不明；重连后确认原控制器仍为 14:14 启动、无维护标记、账本、临时 unit 或演练进程，才通过独立 systemd transient unit 执行本次演练。
- 本次是候选启动测试，不是正式上线。月度门控现仍未启用，原历史未删除，真实额度耗尽时的生产 GPU 分配与运行任务超额继续尚未在这次实机窗口验证。
