# u20 CLI 月度状态显示修正

- 2026-10-31；目标 u20 用户 `u20`；构建和升级脚本来自提交 `6caee18`，基础客户端 `0858424`，分支 `u20-slurm19`。所有旧文件按固定 manifest 哈希检查，复制到 `/home/u20/.local/share/time/candidate-monthly-discovery-1`，仅在 `time_core.py` 补充对服务端 `enforcing` + `enforcement_ready=true` 的识别。旧 core SHA-256 `90a0c11296f30c8c379362198b272777d5d1a710fbef166280831cfeaca6d534`，新 core `972d895cf3a9d7b177a2c4f4668c2e92973b19c42dc61f5d92a6c8fb8d82d83c`。
- 原 `timectl` 和 `time-mcp` 用户包装脚本备份于 `/home/u20/.local/share/time/backup-before-monthly-discovery-2k8r790y`；逐个原子替换后完成健康检查。MCP 包装仍清除 `TIME_MCP_CONSENT_FD`，保持只读；没有变更系统服务、调度器、额度账本或任务。
- 实机 `timectl capabilities` 汇总返回 `server_state=enforcing`、`enforcement_ready=true`，`timectl quota` 返回当前 debug 档；MCP 列出 10 个工具，包含 `time_quota`，未列出提交、取消或审批工具。三个服务 active、Slurm 队列空。客户端元数据中的 `client_support=unsupported` 仍有歧义，尽管 quota 命令可读取账本；不将此条目解释成写操作可用。
- 私有临时文件 fixture 验证了更新成功与第一次包装替换后发生故障时的恢复；生产未发生回滚。恢复时先核验现有包装内容，必要时使用备份恢复，避免覆盖之后的其他更新。
