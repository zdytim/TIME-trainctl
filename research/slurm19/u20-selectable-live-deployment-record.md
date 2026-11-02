# u20 selectable monthly tiers: deployment record

- Target: u20-Z790-S-DDR4 (RTX 4090 test host).
- Activation: 2026-11-02 09:00:07 +08:00.
- Controller source commit: `f417af53729e22bdddb8a54043df80fe07428d32`; executable SHA-256 `47e8210f486f1889a1cf88613ab4a0b5ecc53a003ebb1bc9ab330809ac9db5eb`.
- Platform source commit: `faa28a622fef97cc6d5b4cebd795eb16013bfc44`; deployment procedure commit: `3f4c832`.
- Components: Slurm controller, broker, Cockpit training UI and job submission plugin; V2 monthly ledger activated. The successful activation reset prior unclassified quota consumption; historical job rows remain visible.
- Root-only backup: `/var/backups/pro6000d-training/selectable-c02lyi2f` on u20, including previous components, configuration, database snapshot and ledger state.
- Activation verification: controller and platform started; V1/V2 snapshots and the timzhang platform capability reported selectable tier enforcement. V2 cutover sequence was 16. The preceding attempt was rolled back before ledger cutover and is documented in `u20-selectable-f417-staging-record.md`.
- UI and live scheduler verification: timzhang selected `important` and submitted job 14 `time-tier-check`, requesting one GPU. The job completed; the controller snapshot recorded 12.130480844 GPU seconds in important, zero in debug and normal, and zero active allocations. This was a short Python sleep with a GPU reservation, not a CUDA kernel test.
- Earlier isolated controller verification showed a depleted selected tier remaining pending without GPU allocation, and checked V2 persistence/replay. It did not exercise an actual depleted quota on the live host.
- Rollback outcome: not required for the successful activation. After V2 cutover, recovery must account for the new ledger; do not blindly restore the prior ledger.
- Scope and limitations: this deployment is on u20 only; pro6000d has not been upgraded by this release. Same-user GPU file-descriptor passing remains a known isolation limitation and is not established to be blocked by this release. Per-job UI details may show as unavailable even when aggregate controller accounting is present.
