# TIME Trainctl

TIME Trainctl 是一套共享 GPU 训练管理系统。训练者通过 Cockpit 或 `timectl` 提交任务、查看队列与日志、查询 GPU 小时和申请额度。平台通过 Linux 账户识别用户，由 Slurm 分配 GPU。

## 在已部署的服务器上使用

网页入口位于 Cockpit 左侧的「训练管理」。在 pro6000d，管理员可在「用户与额度」创建训练账户、为已有账户开通训练、管理额度和设置最高优先级。

在服务器上以自己的训练账户运行以下命令。提交前将工作目录和训练命令换成自己的项目。

```sh
timectl whoami
timectl doctor
timectl quota
timectl gpu
timectl queue
timectl validate --tier debug --gpus 1 --workdir "$HOME" -- python train.py
timectl run --tier debug --gpus 1 --workdir "$HOME" -- python train.py
```

任务提交后，在网页任务队列或使用 `timectl show JOB_ID` 查看状态，使用 `timectl logs JOB_ID` 查看日志。选定的月度档位按实际分配的卡数和占用时间计费。等待任务没有 GPU 分配时无需扣费。

## 源码与维护

| 目录 | 内容 |
| --- | --- |
| [pro6000d-live-platform](pro6000d-live-platform/) | pro6000d 的 Cockpit 插件、训练服务、Slurm 26 构建与部署工具 |
| [pro6000d-cli-mcp](pro6000d-cli-mcp/) | `timectl`、只读 MCP 与终端确认入口 |
| [u20-training-platform](u20-training-platform/) | u20 的 Slurm 19 适配和部署工具 |
| [research](research/) | Slurm 19 和 26 的构建补丁、验证材料和发布证据 |
| [deployment-records](deployment-records/) | 已执行的部署、验收与回退记录 |

本仓库提供对应现有部署的源码和维护工具。升级需要匹配目标主机的 Slurm 构建产物和现有配置。pro6000d 和 u20 各自维护调度配置及月度账本。已有任务和账本的保护要求见 [AGENTS.md](AGENTS.md)。

首版功能、已核对的使用流程与待验收项见 [v1.0.0 候选发布说明](docs/releases/v1.0.0.md)。
