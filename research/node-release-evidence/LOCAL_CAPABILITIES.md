# u20 本地能力与可见性（只读审查）

本文件只记录本地非敏感配置和源码位置。没有读取凭据、运行作业数据或服务状态，没有改动 Slurm 配置。

## 已核实

- `/usr/sbin/slurmd -V`：`slurm-wlm 19.05.5`。
- `/etc/slurm-llnl/slurm.conf`：`AuthType=auth/munge`、`CredType=cred/munge`、`StateSaveLocation=/var/lib/slurm/ctld`、`SlurmdSpoolDir=/var/lib/slurm/slurmd`、`SchedulerType=sched/backfill`、`SelectType=select/cons_tres`、`TaskPlugin=task/cgroup`、`ProctrackType=proctrack/cgroup`。
- `/etc/slurm-llnl/cgroup.conf`：`CgroupAutomount=yes`、`ConstrainDevices=yes`、`ConstrainRAMSpace=yes`；`AllowedDevicesFile` 指向系统配置文件。该文件内容未作为本交付输入读取。
- 当前审查进程的 `/proc/self/cgroup` 可见 legacy `devices` 和 `pids` 控制器路径以及 systemd cgroup 路径。这证明本进程能观察到 cgroup 归属字段；不能证明能读取或修改 Slurm 作业 cgroup，也不能证明能证明 PID 生命周期、PID reuse 或 GPU 物理清理。
- `/usr/include/openssl/evp.h` 可读，原型的 SHA-256 代码使用 OpenSSL EVP。没有安装或修改软件包。

## 本地 Slurm 源码定位

只读源码副本 `/home/u20/strict-controller-review-6IbC4Oex/source` 显示：

- `src/slurmctld/job_scheduler.c` 的 `_schedule_job()` 路径调用 `select_nodes()`，约 1670 行。
- `src/plugins/sched/backfill/backfill.c` 的 `_start_job()` 路径调用 `select_nodes()`，约 2731 行。
- `src/slurmd/slurmstepd/mgr.c` 在 `proctrack_g_wait()`、`task_g_post_step()` 后发送 step completion 消息，约 1166-1182 行。
- `src/plugins/task/cgroup/task_cgroup_devices.c` 的 `task_cgroup_devices_fini()` 通过 `xcgroup_delete()` 删除 devices cgroup，约 137-170 行；错误主要记录到 debug/error 日志，未形成本原型可验证的 signed/authenticated cleanup receipt。

因此可以具体定位未来的集成点，但当前工作区没有 Slurm 源码改动、插件、IPC handler 或 scheduler transaction。无法从只读配置证明真实 slurmd peer 身份、Munge credential 的内容、controller witness 的不可回滚高水位、GPU reset/MIG/vGPU 清理或资源选择与 gate 的原子性；这些项保持 UNKNOWN/阻断。

## 当前可安全结论

本地 cgroup 配置说明 Linux 设备隔离能力存在，不能把它提升为“GPU 已释放”的证据。原型在默认设置下拒绝初始化、打开、重放、应用、观察和准入；UNKNOWN 保持 usage held，现有任务不被本交付杀掉或迁移。没有可信证据时新 GPU 分配应由真正的 Slurm controller 集成保持 PENDING；本库本身不提供该保证。
