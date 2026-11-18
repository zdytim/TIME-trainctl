# pro6000d 训练账户开通入口

- 功能提交 `00a4822782932fff5730f4cd1067b001086d80ae` 已推送至 `pro6000d-slurm26`。
- 目标为 `pro6000d`（主机名 `ubuntu`）。北京时间 2026 年 11 月 18 日 22:21 完成安装。
- 更新 Cockpit 的 `/usr/share/cockpit/pro6000d-training/app.js`，安装仅 root 可执行的 `/usr/local/sbin/pro6000d-provision-account`。训练管理页新增「新增训练账户」与「开通已有账户」。
- 备份目录为 `/var/backups/pro6000d-training/account-enrollment-00a4822-esy8ldig`，内含原页面文件 `app.js` 和发布记录 `release.json`。
- 上线前核对了 Python 和 JavaScript 语法。线上核对了文件哈希、Cockpit 插件可见性、管理员的只读账户列表和训练平台调度状态。投产前后训练服务和三项 Slurm 服务的进程 ID 均一致，调度器可用，队列均为空。该次核验未创建或修改账户，也未提交作业。
- 未触发回退。需要回退时，从备份原子恢复 Cockpit 的 `app.js`，然后移除这次新增的 root 助手。已经开通的账户及其 Slurm 关联由管理员另行管理。
