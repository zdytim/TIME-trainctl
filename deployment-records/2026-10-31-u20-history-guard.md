# u20 staged history request protection

- Target: `u20-Z790-S-DDR4`, approximately 2026-10-31 12:28–12:29 Asia/Shanghai.
- Source commit `bc56fb1`, rollback-compatibility validation `36b09a9`, guarded
  rollout `88914c1`; these commits were pushed to `u20-slurm19` before use.
  The installed broker changed from SHA-256
  `4224e6aa116532585c6c9e837ee102e9d508ef00b04e6e685bd5e6f092e88821`
  to `24afd69cff5eac856f2ccaaf8a97b95c1823c911c83f40338745cd4ab813e821`.
- The reviewed rollout backed up the broker and configuration, stopped only
  the training backend, rechecked scheduler and platform job state, took a
  SQLite backup, installed the candidate, reapplied the existing GPU ACL helper
  and restarted the backend. It did not restart Slurm or the NVIDIA driver.
- Backup: `/var/backups/pro6000d-training/u20-gpu-probe-52gvnbz6`, containing
  the previous broker/configuration, post-stop database snapshot and result.
  Automated rollback was not required; the result recorded `healthy; monthly
  gate remains disabled`. The rollback path restores known old code/config and
  retains the live DB, subject to later history/grant reconciliation.
- Validation: the rollout checked an empty scheduler and no active platform
  jobs before and after backend stop, then checked real admin and training-user
  API state, staged monthly status, GPU inventory and device ACLs. An independent
  post-check found the installed hash as above, seven retained history jobs,
  an empty tombstone table, 16 audit entries, empty scheduler queue, all three
  services active, `/dev/nvidia0` `other::---`, and `SlurmctldParameters` unset.
- This installs the broker's tombstone-aware checks and schema only. The
  database triggers are created by the separately staged cleanup primitive;
  they have not been installed on the live DB. No history was deleted, and the
  monthly controller is not active. The standalone cleanup still requires a
  reviewed coordinated maintenance, backup, scheduler fence and rollback.
