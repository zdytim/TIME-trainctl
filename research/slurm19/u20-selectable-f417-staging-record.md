# u20 selectable controller: inactive staging record

- Target: `u20-Z790-S-DDR4`.
- Staging observed at: 2026-11-02 01:14:18 +08:00.
- Controller source/build commit: `f417af53729e22bdddb8a54043df80fe07428d32`; staging procedure commit: `0bebe09`.
- Component: selectable V2 experimental controller and 94 plugins, placed in `/opt/time/slurm19-f417af53729e`. This is an inactive versioned directory; no service or configuration was switched.
- Verification: package manifest `0ce6a721608fc9f6f4668cf9fa54c3c281a5cfcf5d0e012a4c06cce8d15f7bf9` and 96 archive members verified before staging; staged controller SHA-256 `47e8210f486f1889a1cf88613ab4a0b5ecc53a003ebb1bc9ab330809ac9db5eb`. After staging, `slurmctld`, `slurmd`, and `pro6000d-training` were active; `slurmctld` PID remained 1258; `squeue` was empty at that instant.
- Backup: no existing service binary, configuration or ledger was replaced, so no backup was created for this staging action. A separately verified backup is required before an actual cutover.
- Rollback: not needed; the existing controller remained active. The staged candidate is not a deployed or enabled feature. No UI, GPU task or V2 cutover behavior was verified by this staging.
