# u20 cron account guard

- Target and time: `u20-Z790-S-DDR4`, 2026-10-31 approximately 13:39–13:40
  Asia/Shanghai. Candidate guard and isolated PAM check `38a7465`, guarded
  rollout and rollback fixture `d894cd8`, were pushed before installation.
- Added root-owned `/usr/local/libexec/pro6000d-cron-guard` SHA-256
  `e78e145a2b05ff9b7e64ff12cd2555785410e3582975c188042bf7e7f8dc5959`.
  Added one requisite PAM account check to `/etc/pam.d/cron`, after its
  existing training-group rule. New cron PAM SHA-256 is
  `9d2ecaddb980ce080d549290d56cbc38c1b6f26fb02c65aef852a870105afcfb`;
  the prior hash was `48657378ba743ca0bb465d856c122f9ac255fe99ad1b9a37c96a552b9524c816`.
  The helper uses Python isolated mode. Human non-admin accounts are denied
  cron account access whether or not enrolled; platform admins, root, low-UID
  system identities and accounts with non-login shells are exempt. Existing
  training-group cron denial remains. SSH, sudo and systemd-user PAM files were
  not modified.
- Backup: `/var/backups/time-cron-guard.GYV2SZsa/cron.before`, inside a
  root-only 0700 directory. No rollback was needed on the host. The installer
  restores the exact prior PAM file on detected failure and only removes its
  helper when rollback is safe; it refuses to overwrite an unexpected change.
- Local policy checks, a temporary real PAM account stack on u20, and
  networkless isolated rollout and injected post-install rollback fixtures
  passed. The isolated image was `time-cron-guard:d894cd8`, SHA-256
  `dc463591c7d621616c91d0c86a09ba8ba916508d6be2201701d47e4e8604f5a5`.
  After installation, actual `cron` PAM account checks allowed `u20` and root,
  and denied `timzhang` and a nonexistent account. A real cron scheduling
  cycle and an existing un-enrolled human account were not exercised here.
- Post-check: cron, SSH, Slurm and training backend active, Slurm queue empty,
  and GPU/control/UVM ACLs `other::---`. No daemon restart was needed. This
  closes a cron account path for ordinary humans; it does not make GPU device
  permissions immutable or constrain privileged and non-login service accounts.
  Monthly quota enforcement remains disabled.
