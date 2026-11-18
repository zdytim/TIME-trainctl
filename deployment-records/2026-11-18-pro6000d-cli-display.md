# pro6000d CLI 显示升级

- 功能提交 `d9df84bf95c0b96961e8dca6fa073c67d08b4325` 已推送至 `pro6000d-slurm26`。
- 目标 pro6000d（主机名 `ubuntu`）。于北京时间 2026-11-18 18:32 完成安装和线上核对。
- 使用两个 Git 归档校验新旧客户端文件。共享入口已切换至 `/usr/local/lib/pro6000d-client/20261118-cli-display-v3`。上一个版本 `20261117-unlimited-v2` 保留，入口备份在 `/usr/local/lib/pro6000d-client/20261118-cli-display-v3-backups`，核对过的发布源在 `/var/backups/pro6000d-training/cli-display-d9df84b-8ut80f9t`。
- 线上只读核对：管理账户可用 4 张卡，表格显示 4 张。一个可用额度为 1 张的空闲普通账户，表格与 JSON 均显示 1 张。JSON 的过程数据、历史采样和 `metrics.gpu` 也在同一可见范围。月额度栏以 GPU 小时显示。三项生产服务均 active，队列中 1 个运行任务继续运行。此次未重启服务。
- 未回退。若需恢复旧 CLI，可核对当前三条共享入口后，通过版本目录与已保存的入口备份恢复，无需更改调度或账本。
