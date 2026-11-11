# pro6000d 的 timectl 和 MCP

`timectl` 和 `time-mcp` 以当前 Unix 账户身份连接训练平台的本地 socket；不会直接调用 Slurm 或绕过月度额度。用户须属于平台授权的账户或组。

```sh
timectl whoami
timectl quota
timectl queue
timectl validate --tier debug --gpus 1 --time 10m --workdir "$HOME" -- python train.py
timectl run --tier normal --gpus 1 --time 1h --workdir "$HOME" -- python train.py
```

`--tier` 是必填的月度额度档位，`--qos` 是另外的单次时限档位。执行会在终端要求确认；非交互式使用须明确传 `--yes`。命令通过 Bash 执行，不能将其当作原生 argv 执行。任务 ID 和不确定结果保存在用户私有目录，可用 `timectl request-status REQUEST_ID` 核对，避免重复提交。

`time-mcp` 是标准输入输出 MCP 服务，默认只暴露只读工具。`time-mcp-confirm` 在有控制终端时为提交、取消和申请等写操作显示完整请求并要求逐次确认；没有终端时退回只读。MCP 客户端必须在该用户自己的服务器会话中启动，不应共享 root 进程。模型传入的确认字段不能代替人工确认。

终端确认用于防止误操作，不能抵御已经取得同一 Unix 账户执行权限的恶意程序：同一账户本身也可以调用 CLI。需要隔离不可信 AI 程序时，应使用独立账户和权限边界。

月度追加申请示例：`timectl request create --kind monthly_gpu_hours --gpu-hours 10 --month 2026-11 --reason '项目需要额外额度'`。服务器会核对当前北京时间月份；月份切换时需要刷新后重新申请。
