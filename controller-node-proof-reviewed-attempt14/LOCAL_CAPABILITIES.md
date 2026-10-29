# Read-only u20 capability evidence

Collected on 2026-10-29 from the local namespace only. No credentials,
active Slurm jobs, controller RPCs, privileged commands, service changes, or
host changes were used.

## Observed

* `slurmd -V` reports `slurm-wlm 19.05.5`.
* `/proc/self/cgroup` exposes legacy `devices` and `pids` controllers and a
  systemd cgroup path. A visible cgroup v2 mount at `/sys/fs/cgroup/unified`
  is read-only in this namespace.
* The visible legacy devices hierarchy contains `devices.list`,
  `devices.allow`, `devices.deny`, `cgroup.procs`, and a Slurm-named
  node directory. This proves only that a legacy devices hierarchy is visible,
  not that this process can inspect or mutate a job cgroup safely.
* `/proc/filesystems` exposes both `cgroup` and `cgroup2`.
* `/proc/sys/kernel/random/boot_id` is readable. A real node verifier would
  have to bind the boot identity to an authenticated slurmd message.
* The visible `/dev` listing did not expose a GPU character device to this
  namespace. This cannot establish physical GPU inventory or release.
* `/etc/slurm`, `slurm.conf`, `cgroup.conf`, and `gres.conf` were
  unavailable in the workspace namespace. The configured cgroup plugin, GRES
  mapping, cgroup version, privileged slurmd policy, and authenticated IPC
  details are unknown.

## Consequence

There is no local reliable basis for positive GPU cleanup proof or process
attribution proof. The prototype consequently requires an actual privileged
slurmd integration to authenticate cgroup identity, PID start-time/PID-reuse
checks, and exact device release. If that evidence cannot be produced, the
result remains `UNKNOWN` and usage remains held.

