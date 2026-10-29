# 第 14 轮静态复审报告

结论：本版本仍为默认禁用、未集成的研究原型。`durable_backend_ready()` 在 `src/release_evidence.c` 中无条件返回 false，公开 API 返回 `RE_PROOF_UNAVAILABLE`；没有生产部署、Slurm 月度门控或可信 GPU 释放证明。

## 逐项映射

| 审查项 | 当前源代码 | 结论与失败关闭语义 |
|---|---|---|
| 1. FAULT 未绑定完整上下文、链头可由调用者提供 | `include/release_evidence.h:105-127`；`src/release_evidence.c:497-634` 的 `hash_context`、`fault_context`、`event_context`、`witness_fault` | 已修复接口形状和源代码摘要：完整 fault 字段进入 canonical SHA-256，链头由 `chain_head_from_digest` 重算，重放再次验证。`verify_commit` 现在接收完整 `re_commit_context`。外部见证、签名和反回滚仍缺失，故不构成证明；backend fence 保持阻断。 |
| 2. shape/prev-chain 失败只在内存故障 | `src/release_evidence.c:1304-1323`、`1155-1292`、`1406-1411`、`1478-1497` | 未认证 shape/auth 拒绝且不让不可信输入写 FAULT；已认证前链冲突可生成完整 FAULT。任何 witness、append 或 fsync 歧义都把当前 store 置 UNKNOWN/recovery_failed，但普通文件无法证明重启后的隔离仍存在。需要外部 durable quarantine/high-water；未实现，不能恢复。 |
| 3. witness、认证、GPU collector、节点恢复、Slurm 原子 gate、256 上限 | `src/release_evidence.c:29-30`、`878-970`、`1772-1885`；`LOCAL_CAPABILITIES.md` | 256 event index 和 256 node/boot tracker 无安全压缩，耗尽返回忙/失败关闭。公开 gate 只是 mutex 内谓词；没有 Slurm handler、可信 slurmd IPC、collector 或 controller witness。保持 disabled，不能满足 GPU 分配前排队。 |
| 4. 当前实现的精确边界 | `src/release_evidence.c:481-485`、`1629-1885` | `RE_PROOF_UNAVAILABLE` 是明确的 review fence，不是回调成功的替代品。历史输出目录未改动；本目录只交付源代码、设计、能力记录和静态检查证据。 |

## 设计权衡

- 对 shape 错误不持久化 attacker-controlled fault，避免不可信输入伪造 controller 隔离；代价是若未来没有可信 controller quarantine，重启无法记住该拒绝，必须继续拒绝恢复。
- 对已认证身份冲突同时封锁可信旧身份、事件身份和相关 allocation，扩大隔离范围以避免错封一个节点导致另一个节点仍可分配。没有可信原身份时应升级到整个资源域；当前没有执行该屏障的 Slurm 代码。
- 先 PREPARE fsync、见证、COMMIT fsync、再更新内存，能避免正常路径提前发布 RELEASED；它不能解决 fsync 返回错误时“已落盘还是未落盘”的两将状态。需要外部原子 witness 才能消歧。
- 固定 256 条索引保守地失败关闭，避免无界内存；没有安全高水位和摘要归档前不能回收旧条目，因此这是活性阻断。

## 未完成和不可声称的保证

1. 没有可信末尾序列、anchor/chain-head 签名绑定、不可回滚持久 high-water 或崩溃恢复 witness；日志截断风险仍存在。
2. 没有真实认证 transport。配置显示 Munge，但本原型没有复用 Slurm IPC，也没有读取或验证任何 credential。
3. 没有 privileged slurmd cleanup collector。Linux cgroup devices 配置和 `xcgroup_delete()` 只能说明隔离/删除调用位置，不能证明物理 GPU reset、设备句柄、MIG/vGPU 或进程竞态已清理。
4. 没有与 Slurm 19.05.5 的 `select_nodes()`、cons_tres 资源选择、job state 持久化和 StateSaveLocation 同一事务的 admission gate。库谓词不能保证额度耗尽后在 GPU 分配前排队。
5. 节点重启的整节点代次/租约 barrier、可信管理员恢复、pending PREPARE 修复和 256 项安全压缩没有完整实现。管理员授权不能替代 cleanup evidence；恢复必须验证旧 generation、node/boot、peer、external witness 和正向清理证据。
6. 正常运行任务由本原型保持不变；UNKNOWN 保留 usage，不能作为释放依据。无证据时安全结果是拒绝恢复和阻止新准入，而不是提前释放。

## 静态检查证据

仅运行隔离语法/编译检查，没有运行测试、模拟、服务、作业或部署：

```text
cc -Iinclude -DRELEASE_EVIDENCE_ENABLED=0 -std=c11 -O2 -Wall -Wextra -Werror -pedantic -pthread -fsyntax-only src/release_evidence.c   # exit 0
cc -Iinclude -DRELEASE_EVIDENCE_ENABLED=1 -std=c11 -O2 -Wall -Wextra -Werror -pedantic -pthread -fsyntax-only src/release_evidence.c   # exit 0
```

Makefile 的 `compile-check` 只做语法检查和临时 object 编译后删除；没有链接、测试或安装动作。OpenSSL EVP 头文件在本地可读，但本交付没有生成二进制，也没有宣称运行时依赖已经部署。
