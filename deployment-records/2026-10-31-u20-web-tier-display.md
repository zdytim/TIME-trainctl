# u20 网页训练验收与档位显示修正

- 2026-10-31 17:35–17:38 CST；u20 RTX 4090。以 timzhang 从网页提交任务 12 `time-web-postboot-probe`，1 GPU、2 分钟时限；任务完成，网页日志显示 `TIME_WEB_GPU_OK NVIDIA GeForce RTX 4090 16.0`。队列空闲，slurmctld、slurmd、pro6000d-training 均 active，检查时 GPU 设备 ACL other 为 `---`。
- 发现页面把旧的 qos 时限字段展示为实际调度档：表单隐藏字段固定 normal，而账户当前额度档是 debug；旧字段并非控制器的实际月度调度档证据。源码修正提交 `6a33558`，部署前已推送 `u20-slurm19`。月度门控启用时改为显示“自动分档（任务档位暂无明细）”，未更改控制器或额度。
- 17:37 部署 `/usr/share/cockpit/pro6000d-training/app.js`，新 SHA-256 `7e0253f7bedf269151db86e94c609cb7287ceb2e884899bce00561f8d89240db`；旧文件备份 `/var/backups/pro6000d-training/tier-display-7N2Uuleu/app.js.before`，旧 SHA-256 `49ea825569063d09ed53be85f7363ef3bc15f342951a137f1cdb5c2b34840e09`。JS 语法检查通过，服务均 active，未重启服务。
- 当前 Cockpit 会话刷新后仍显示旧内容，文件已更新但页面生效尚未验证；需新建登录会话核实缓存，不应宣称修复已对用户生效。未执行回滚；回滚时先核实当前哈希为上述新值，再原子恢复备份并验证旧哈希。
- 单任务 GPU 小时及实际历史调度档尚无可靠投影；不能从旧字段推断。设备权限曾被驱动自动重置，永久修复仍待验证。
