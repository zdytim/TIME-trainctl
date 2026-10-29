# 第 14 轮节点释放证据原型设计

本目录是独立审查版本。`RELEASE_EVIDENCE_ENABLED` 默认值为 `0`，并且 `durable_backend_ready()` 永久返回 false；所有公开存储、重放、应用、观察和准入接口都先返回 `RE_PROOF_UNAVAILABLE`。即使调用者传入回调，也不能把本地文件和占位回调变成可用证明。原型没有接入 Slurm，没有可信 slurmd 身份传输，没有 GPU 清理采集器，也没有实际持久见证服务。不得部署或宣称月度门控已启用。

## 本轮实现

`re_commit_context` 现在公开承载完整的提交见证内容：主、次 allocation key，event id，reason，node sequence，durable record count，前链头，计算出的记录链头和摘要，两个 node/boot identity，以及三个 block 标志。`fault_context()` 对完整 FAULT 上下文做固定字段顺序、明确长度、无结构填充的 SHA-256 编码；`event_context()` 对事件字段、cleanup 证据、负面信号和 authenticated bytes 做同样编码。实现使用 OpenSSL EVP SHA-256，摘要不是调用者提供的值。

记录摘要的链头由 `chain_head_from_digest()` 计算为固定域、journal version、初始化 anchor 和初始化链头、前链头、持久记录序号、记录摘要的 SHA-256。重放时 `witness_fault()` 重新计算完整 FAULT 上下文和链头，再交给 `verify_commit`；事件同样在见证前重算并比对输入链头。这样修复了旧接口只把 fault 的部分身份传给 verifier、以及调用者可以任意声称链头的问题。见证回调仍只是接口：本目录没有外部见证、签名密钥或回滚保护，因此不能将它当作安全保证。

未认证的 shape 错误和 `verify_event` 拒绝在 `apply_event_locked()` 中直接返回，不由不可信输入生成持久 FAULT。已经通过事件认证但前链头不符、身份冲突、序号冲突或状态转换异常的路径，会以当前可信 store 链头构造完整 FAULT；FAULT 见证、追加、状态标记任一失败都保持 `recovery_failed` 和全部 allocation UNKNOWN。状态发布位于追加 commit 之后，并受进程内 mutex 保护；重放必须先完整通过初始化、PREPARE/COMMIT 配对、FAULT 链、见证和 recovery verifier 才能把 store 标记为 recovered。

所有外部字符串在 `event_shape_valid()` 和持久 FAULT 重放中先检查终止符、长度和必要非空条件，再进入 `field_equal()`；START 拒绝 negative evidence、controller partition 和 admin flag。FINALIZATION_REQUESTED 携带这些信号会保持 UNKNOWN；CLEANUP_ATTESTED 和 ADMIN_RECOVERY 需要 cgroup identity、inode、双扫描 token、PID reuse/starttime 检查、空 cgroup、cleanup 完成和 device release positive evidence。UNKNOWN 始终保持 usage held。节点、boot、peer 或 allocation 身份冲突会把已知状态和冲突身份置为 UNKNOWN，并阻止相应 node/boot；这仍不等于真实节点隔离。

## 审查阻断和设计边界

1. **外部防回滚。** `verify_recovery(anchor, count, chain_head)` 规定了恢复时需要的外部高水位，但当前 options 仍可由调用者提供初始化 anchor/chain head，接口没有实现可信尾序列、链头与 anchor 的签名绑定，也没有 crash-consistent witness commit。日志截断到初始化记录或已提交前缀无法由本地代码发现。若没有 controller 的不可回滚 durable witness（例如与 journal commit 绑定的认证高水位、旧值拒绝和明确的故障恢复协议），恢复必须拒绝并继续 UNKNOWN；这也是永久禁用的原因。

2. **FAULT 绑定和错误持久化。** 见证输入现在包含 fault node、secondary node、boot、block flags、secondary key、reason、sequence、count、前链头和计算摘要，源代码会重建这些内容。可是 `append_fault_record()`、`append_commit_record()` 仍是普通 write/fsync；写入后 fsync 返回错误时，记录是否已到稳定介质有歧义。内存 recovery_failed 可能在重启丢失，而完整 PREPARE+COMMIT 可能被重放接受。没有外部 witness 的 prepare/commit/fence 协议和可恢复的提交状态证明，就不能声称持久故障隔离。当前永久 backend fence 把这类路径整体挡住。

3. **未认证输入与已认证异常。** 不可信 shape 或身份认证失败不会写 FAULT，避免攻击者利用坏输入制造可见隔离记录。已认证但链、序列或身份冲突的事件才可构造持久 FAULT。若见证或追加失败，源代码只在当前进程进入 recovery_failed；安全的上线实现必须另有 controller durable quarantine record，并把失败不确定性写入外部高水位。当前没有该存储，所以重启安全性被视为未知而拒绝恢复。

4. **节点 A/B 绑定。** `durable_identity_fault()` 把可信已有 lease 的 node/boot 作为 primary，同时把新事件身份作为 secondary；event-id 冲突还绑定旧事件 node/boot 和 secondary allocation。此策略宁可扩大隔离范围，也不把冲突事件仅归因于事件声明的节点。若可信原 node 不存在或 witness 无法确认，安全范围只能扩大到整个 controller 资源域，不能只封锁 B；当前原型没有能执行该集群级屏障的 Slurm 集成。

5. **PREPARE、COMMIT 和恢复。** PREPARE fsync 后才调用 commit witness，COMMIT fsync 后才公布内存状态。崩溃发生在两者之间时，重放看到 pending PREPARE 会拒绝服务；COMMIT 已写入但 fsync 失败时无法判断提交。应由 controller witness 原子记录 allocation generation、前链头、commit digest 和 fenced high-water，并提供只增不回退的查询；没有该见证，不能通过清除 pending 或人工忽略 fsync 错误恢复。

6. **索引治理。** `RE_MAX_IDEMPOTENCY` 和 node/boot tracker 仍是 256。没有保留摘要、历史冲突检测、稳定高水位和外部归档的安全压缩协议。达到上限时源代码进入 UNKNOWN/失败关闭，可能阻塞整个 store；这是上线阻断，不是可以靠循环或丢弃旧项解决的活性问题。未来只能在证明旧 event id 已由外部高水位永久封存、冲突摘要可重建且压缩记录已稳定提交后压缩。

7. **节点重启和现有任务。** 当前状态转换可以将所携带 allocation 置 UNKNOWN；真实节点级 restart barrier、整节点所有 generation 租约扫描、跨 boot 的持久隔离屏障仍未实现。失败关闭保留已有运行任务和 usage accounting，不杀任务、不删除 cgroup、不释放 GPU；只有可信 node identity 加 verified cleanup evidence 才能改变单一 lease 的释放状态。

8. **准入原子性。** `re_store_admission_gate()` 只是 mutex 内瞬时谓词，不能与 Slurm 的资源选择、cons_tres 分配、controller StateSaveLocation 和 job state commit 原子关联。可落地的未来集成必须在 `select_nodes()` 选择 GPU 之前，在 slurmctld 持有同一调度/资源锁的事务中读取持久 node fence，把 gate 结果与资源选择和 job RUNNING 持久化一起提交；失败、partition、节点重启、witness 不可达都保持 PENDING，并让已运行任务继续运行。这个改动未实现，因此该 API 不能满足“额度耗尽后在 GPU 分配前始终排队”。

## 安全边界

可信边界应是 slurmctld 的 controller durable journal/witness、通过 Slurm 已认证 IPC 证明的 privileged slurmd，以及由节点特权组件采集的 cgroup/device/process evidence。用户进程、普通 epilog、日志文本、单次 PID 列表或本库回调不能成为释放证明。Linux cgroup 删除成功也只能证明内核层级操作的返回结果，不能单独证明物理 GPU reset、设备句柄关闭、MIG/vGPU 状态或其他进程没有竞态残留。

当前 u20 配置显示 Slurm 19.05.5 使用 `auth/munge`、`cred/munge`、`task/cgroup`、`proctrack/cgroup`、`select/cons_tres` 和 `sched/backfill`，并启用 `ConstrainDevices=yes`。本地 Slurm 源码有 `task_cgroup_devices_fini()` 的 cgroup 删除路径，也有 `proctrack_g_wait()` 和 step completion RPC；这些路径没有向本原型提供认证、链式持久见证或物理 GPU 清理证明。上述事实只支持集成定位，不支持生产安全结论。

本版本只保留可静态审查的接口和失败关闭逻辑。未实现的见证服务、真实 peer authentication、节点恢复协议、GPU collector、Slurm 调度事务、索引压缩和可证明 crash recovery 必须由后续受审查的独立变更完成；在此之前，`RELEASE_EVIDENCE_ENABLED=0` 和 durable backend fence 必须保持不变。
