# u20 controller configuration and launcher: inert stage

- Target and time: `u20-Z790-S-DDR4`, 2026-10-31 approximately 12:33–12:34
  Asia/Shanghai. Stage code commit `43ef194`, config generator `32121e2`,
  and controller candidate `7a06cff` were pushed to `u20-slurm19` first.
- Created `/etc/slurm-llnl/time-controller-7a06.conf` (root:root 0644,
  SHA-256 `8ce457b2deec7c42438a06db2360d01163e681dea351d05a68bdacc8ab658411`)
  and `/usr/local/libexec/time-quota-anchor-7a06` (root:root 0755,
  SHA-256 `b705fcb6c16739bd6b283f137399c66cbc0ea642d1e0155110efd4ef8c854ba8`).
  Both names were new; no existing files were overwritten, so no replacement
  backup was needed. No unit, running service or active configuration changed.
- The staging tool verified the baseline `/etc/slurm-llnl/slurm.conf` SHA-256
  `6c6072b71804357a06c270f883981f95b9b2ae4ce35913026b42956d69172c63`,
  checked all 99 release members against the pinned manifest, and wrote both
  files using exclusive creation. Three private staging tests and five config
  validation tests passed before deployment.
- Afterward the Slurm queue was empty, `slurmctld`, `slurmd`, and the training
  backend were active; `slurmctld` retained PID `2937181`, the original
  `/usr/sbin/slurmctld` unit command, and unset `SlurmctldParameters`. Monthly
  enforcement remains disabled and these new files are not referenced by the
  current unit. No rollback was needed or attempted. Future cleanup must first
  verify no service or configuration references either file.
- This stage does not create the ledger or witness, install a systemd override,
  delete old jobs, establish device isolation, or prove a safe live switch and
  rollback. A cutover must recheck configuration and release hashes, protect
  running jobs and back up live state immediately before changing services.
