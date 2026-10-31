# u20 real-GPU split-plugin test and ACL restoration

- Target and time: `u20-Z790-S-DDR4`, 2026-10-31 approximately 13:07–13:09
  Asia/Shanghai. The previously committed isolated fixture (`328bec3`) and
  CUDA driver probe were used; image `time-real-stock:772e45c` SHA-256
  `cad30b548093e0ef9cf02034b48b2ecd584519044e0b31cfc826f7f069b70cc2`
  added the committed CUDA probe to the split-plugin image. Container state,
  scheduler configuration, ledger and broker database were private.
- The stock node daemon loaded plugins from its separate system-plugin copy.
  A synthetic exhausted UID 1001 request remained PENDING with no node; after
  a broker grant it ran as UID 1001 and completed a real 1 MiB NVIDIA RTX 4090
  CUDA memory round trip. This test used `task/none` inside the container and
  does not establish production cgroup isolation or power-loss recovery.
- **Device permission regression observed:** on the first host post-check,
  `/dev/nvidia0`, `/dev/nvidiactl` and `/dev/nvidia-uvm` all showed
  `other::rw-`. Before the test they were restricted by the previous platform
  ACL configuration. The existing root-owned
  `/usr/local/libexec/pro6000d-gpu-acl` helper was run immediately after this
  observation; independent subsequent checks showed `other::---` on all three
  devices. This is a restoration of the existing ACL policy, not a permanent
  fix. The GPU container/runtime activity is temporally associated with the
  reset, but this observation alone does not isolate its exact cause.
- After restoration Slurm and the training backend were active, the host queue
  was empty, Sunshine remained as PID `130919` using 384 MiB, controller PID
  remained `2937181`, and `SlurmctldParameters` remained unset. The configured
  `user-1001.slice` policy was `closed`, but its `ControlGroup` was empty at
  inspection; no claim is made about an active user session during the exposure
  interval. No scheduled user job was cancelled or restarted.
- No code/configuration backup was needed for the fixture. ACL restoration used
  the existing helper; there was no rollback of controller or backend code.
  Until device-node permission recreation is addressed and verified across
  relevant paths, do not describe direct-device bypass prevention as durable.
  Monthly enforcement is not active on the host.
