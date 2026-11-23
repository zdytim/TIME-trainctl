# pro6000d 普通作业取消修复

- 已合并的功能提交为 `3f005ccf444f0f98c6cefab058d92fa22b03eaa4`，目标分支 `pro6000d-slurm26`，见 [PR #1](https://github.com/zdytim/TIME-trainctl/pull/1)。
- 目标为 pro6000d（系统主机名 `ubuntu`）。北京时间 2026 年 11 月 23 日 14:47:06 完成上线。
- 从已合并提交中提取并原子替换 `/usr/local/lib/pro6000d-training/pro6000d-training-api.py`，仅重启训练 API 服务。旧文件 SHA-256 为 `e6824de39bbae3674b4c4e5a432986303bcf64845e4117627444be937610eea3`，新文件为 `099f3fb5d71760d716f854134cfbe03462de98c47b5731a624737acc88d50042`。
- 原文件与发布清单的备份目录为 `/var/backups/pro6000d-training/cancel-job-3f005cc-20261123T064704Z-93xmv6m_`。
- 上线前核对 Python 语法、Git 差异与原文件哈希。上线后通过普通训练账号确认调度器可用、月度门控处于 enforcing、三档额度可用。slurmctld、slurmd、slurmdbd 的进程 ID 保持不变，作业 102 在 API 重启前后均为 RUNNING。
- 随后的获授权切换通过新版 API 取消作业 102，平台先返回已请求取消，随后与 Slurm 核对为 CANCELLED，个人 GPU 预留归零。未启动额外验证作业。
- 未触发回退。需要回退时原子恢复备份中的 API 文件，只重启训练 API 服务，并复查调度器、月度门控与活动作业。保留现有数据库和调度器状态。
