# Decision options

| Option | Strict policy | Engineering required | Risk | Decision |
|---|---|---|---|---|
| Keep current adapter disabled | Safe fail-closed; no GPU allocations through this feature | None beyond review and operational isolation | No quota enforcement | Available now |
| Design A core/select/GRES transaction | Potentially strict | New Slurm core ABI, staged cons_tres/GRES state, burst-buffer/preemption participation, durable recovery, target builds, behavior validation | High implementation and ABI scope | Recommended engineering path |
| Design B bounded reservation | Strict only with a real finite bound and global gate | Enforceable scheduler deadline, global resource gate, rollback, all-path integration | Current Slurm19 cannot prove bound | Reject until proven |
| External GPU broker/cgroup owner | Can be strict if broker owns device allocation and cleanup | Change resource architecture and configured semantics | Operational integration risk | Alternative architecture |
| Deny-all guard when enabled | Never allocates an exhausted user's new GPU job | A trivial gate with no positive path | Does not deliver requested service | Safe emergency mode only |

No option permits enabling the current adapter. The u20 facts supplied for review (`accounting_storage=none`, no GPU in `AccountingStorageTRES` at the last check, and missing PAM/munge in the isolated build) also block a production claim until independently validated by the supervisor.
