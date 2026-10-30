# u20 运行基线（2026-10-30）

只读核对：u20 主机 `u20-Z790-S-DDR4` 的 `slurmctld`、`slurmd`、`cockpit.socket` 和 `pro6000d-training.service` 处于 active；当次 `squeue` 无输出，仅代表查询时无排队或运行记录。Slurm 报告版本 19.05.5。

| 内容 | u20 运行文件 SHA-256 | 本计划开始时 Git 版本 SHA-256 |
|---|---|---|
| 平台后端 `pro6000d-training-api.py` | `65d213100a0e0ebd3f9fe87b51d1625f8539acf954ca572b43fa48a07c43173c` | `5186cf43d5371925314230ef4e4429cc5302ec6b40fe5ba88b4419adb46d037d` |
| Cockpit `app.js` | `cf9f2b5bed41e944552c9d9e06a24ffd2eb4b6cc3e8c8fa26ed253cc6a23e067` | `57399464a19a3b87968baf7d49309f405dc8a928ac06fa94024fb811f9648626` |

两份运行文件已在本机非 Git handoff 目录保存以审查。初步差异显示 Git 版本包含月度账本及 UI 的 staged 代码，u20 运行版本没有相应部分；这不是生产启用证据。不能单独用 Git 版本覆盖运行前端，否则可能与现有后端的状态字段不匹配。后续先做完整差异审查，制定匹配的最小发布与备份方案，不修改运行服务或作业。

上述状态是一次查询快照，不能推断后续仍无任务。没有访问运行数据库或凭据，没有测试或部署。
