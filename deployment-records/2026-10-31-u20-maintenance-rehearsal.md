# u20 maintenance fence rehearsal

- Target and time: `u20-Z790-S-DDR4`, 2026-10-31 approximately 13:03–13:04
  Asia/Shanghai. Rehearsal code and marker fixture commit `93c02da` was pushed
  to `u20-slurm19` before the host operation.
- After the read-only preflight and under the existing upgrade lock, the
  script created a root-owned, private, fsynced maintenance marker, stopped
  only `pro6000d-training.service`, confirmed no scheduler or active platform
  jobs, and requested a backend start while the marker existed. systemd left
  the backend inactive with PID 0. The script then removed its own verified
  marker, started the existing backend and verified actual administrator and
  training-user API state. No controller, node daemon, GPU driver, ledger,
  configuration or history record was changed.
- No backup was required for this rehearsal because no existing code,
  configuration or database was intentionally modified. Automatic recovery was
  not needed; normal release removed the marker and restarted the backend.
  The failure handler attempts to remove only the exact marker it created and
  restore the old backend; an uncatchable interruption can leave the durable
  marker and requires inspection instead of an automatic unsafe release.
- Four private marker tests covered create/remove, refusal to overwrite or
  remove a changed marker, and cleanup after a simulated write failure. The
  live exercise printed `MAINTENANCE_REHEARSAL_OK`, including checks that
  backend startup was blocked during the marker and healthy afterwards.
- Independent post-check: marker absent, queue empty, Slurm and backend active,
  controller PID remained `2937181`, backend restarted as PID `1556012`, and
  `/dev/nvidia0` retained `other::---`. `SlurmctldParameters` was unset, so
  monthly enforcement remains disabled. This rehearsal does not fence root
  submissions, simulate a reboot/power loss, or implement controller cutover.
