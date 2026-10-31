# u20 enrollment guard for human login sessions

- Target and time: `u20-Z790-S-DDR4`, approximately 2026-10-31 13:26–13:27
  Asia/Shanghai. Guard and tests commit `0702fda`; account inventory precheck
  `7a004e9`. Both were pushed to `u20-slurm19` before deployment.
- Replaced only `/usr/local/libexec/pro6000d-session-guard` SHA-256
  `274a70e98183d0055d063c19f4eb24a0569d883788fe2f98bcf2545417a0ecab` with
  `6bbfda5e28e14c2097268a5087f7142e5e3654bf49a7d1fef351dfb828beee99`.
  A human login account with UID at least 1000 must now be a platform admin or
  enrolled in the training group; enrolled non-admins must also pass the
  existing cgroup v1 GPU-denial check. Root, low-UID system accounts and
  accounts whose shell is `nologin`, `false`, `sync`, `shutdown` or `halt`
  are exempt. New ordinary users must be reviewed and enrolled before login.
- The guarded rollout confirmed that the only human login accounts were the
  expected `u20` and `timzhang`, checked their admin/training memberships and
  required an empty Slurm queue. It made a root-only backup at
  `/var/backups/time-enrollment-guard.56rh9Hxt/session-guard.before`. Rollback
  was not needed; on a detected post-install failure the rollout restores the
  prior hash-checked guard. PAM stack files themselves were not replaced.
- Four local identity-policy fixtures passed, including an un-enrolled human
  denial, administrator and root exemption, and missing-group refusal; the
  existing slice/device-rule fixture passed. A temporary PAM service including
  the host's actual `common-session` accepted `timzhang` in the intended
  session and rejected an intentionally failing module; the release repeated
  this after installing. Direct post-checks allowed administrator `u20` and
  rejected an unknown username. An initial pre-install fixture failed because
  `/run` is mounted noexec; it did not modify the live guard and was rerun
  successfully from a root-owned executable directory. A complete fresh SSH or
  Cockpit login for every account was not exercised.
- Post-check: Slurm and backend active, scheduler queue empty, the three
  existing NVIDIA device ACLs had `other::---`, and Sunshine PID `130919`
  remained. Monthly enforcement remained disabled. This change narrows human
  PAM login exposure; it does not make device-node permissions immutable or
  cover privileged users, already-running processes, system-service accounts,
  paths without this PAM session stack, or future device/cgroup changes.
