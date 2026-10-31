# u20 共享 CLI 月额能力说明修正

- 2026-10-31，主机 `u20-Z790-S-DDR4`；代码修正提交 `8bd51f0`，针对现有共享安装的部署脚本提交 `92a94f6`，均先推送至 `u20-slurm19`。
- 仅将 `/usr/local/lib/time-cli/0858424-monthly-discovery-1/time_core.py` 中月额能力的 `client_support` 从 `unsupported` 修为 `read`；共享 CLI 和只读 MCP 使用这一核心模块，不改变调度、账本或 MCP 写权限。用户私有 CLI 未更新，且可能在 PATH 中优先于共享命令。
- 原文件 SHA-256 `972d895cf3a9d7b177a2c4f4668c2e92973b19c42dc61f5d92a6c8fb8d82d83c`；新文件 SHA-256 `fc40aa84919e795e72c19c4cb6a7ba1fce5d7a9fae4fc10f70b124beab6ac1ea`；原文件备份 `/var/backups/pro6000d-training/shared-cli-quota-read-dr5hx62x/time_core.py.before`。这项修改使此前共享版本目录中的该文件不再具有旧发布记录中的哈希。
- 安装后以 u20、timzhang 各自身份调用 `/usr/local/bin/timectl capabilities` 均看到 `client_support=read`、`server_state=enforcing`、`enforcement_ready=true`；两个身份的 `quota` 均成功读取 `mode=enforcing`。三个服务 active，队列为空。本次未执行 MCP 协议或训练提交验证，亦未验证用户私有 CLI。
- 未回滚。如需回滚，先核对目标仍为新哈希，再将上述备份以 root 原子恢复并校验原哈希；保留账本和正在运行任务。
