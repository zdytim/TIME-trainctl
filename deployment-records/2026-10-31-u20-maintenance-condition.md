# u20 training backend maintenance condition

- Target and time: `u20-Z790-S-DDR4`, 2026-10-31 approximately 12:56–12:58
  Asia/Shanghai. Condition commit `5cf380c`, bounded transient-unit validation
  `f40fe09`, and installer `9bab19a` were pushed before installation.
- Created `/etc/systemd/system/pro6000d-training.service.d/time-maintenance.conf`
  with SHA-256 `f2d32e2a11e6a15b069fbd7faf5ee41737e520bff245d0c762fb82ebe8b51233`.
  The drop-in adds `ConditionPathExists=!/var/lib/pro6000d-training/time-monthly-maintenance`.
  The marker was absent throughout; no service was stopped or restarted.
- The installer verified exact previous unit/drop-in hashes, absence of the
  marker and target, and an active backend. It created a new root-owned file
  exclusively, reloaded systemd and verified the loaded drop-in set and
  unchanged backend PID. No prior target existed, so no file backup was needed.
  Rollback was not required; on an installer failure its guarded path removes
  only the newly created file and reloads systemd. Private fixtures covered
  successful install, existing file/marker refusal and rollback on PID change.
- On u20, two isolated transient units verified that a marker prevented a
  harmless command from executing and that removing it allowed the command.
  The first attempt could not inspect a garbage-collected transient unit; a
  later test hung because `--wait` was combined with `RemainAfterExit=yes`.
  Only that transient unit was stopped, the test was corrected and bounded,
  and the final test passed. This does not test an actual reboot or prove that
  already-running processes stop when a marker appears.
- Post-check: marker absent, backend PID unchanged at `1544043`, both backend
  drop-ins loaded, scheduler queue empty, `slurmctld`, `slurmd` and backend
  active, and `SlurmctldParameters` unset. The condition has no effect on an
  already-running backend; a future cutover must create the marker, explicitly
  stop and verify the backend, and recheck jobs. Root or other privileged
  submitters remain outside this fence. Monthly quota remains disabled.
