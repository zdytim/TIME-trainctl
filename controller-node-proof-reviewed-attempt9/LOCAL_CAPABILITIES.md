# Read-only u20 capability evidence

Collected on 2026-10-29 from the workspace's local namespace. No credentials,
Slurm jobs, controller RPCs, service changes, or privileged commands were used.

## Observed

* `slurmd -V` reports `slurm-wlm 19.05.5`.
* `/proc/self/cgroup` shows legacy `devices` and `pids` controllers and a
  systemd cgroup path. The visible cgroup v2 mount is
  `/sys/fs/cgroup/unified`, mounted read-only in this namespace.
* The visible legacy devices hierarchy contains `devices.list`,
  `devices.allow`, `devices.deny`, `cgroup.procs`, and a Slurm-named node
  directory. This establishes that a legacy devices controller exists in the
  host-visible namespace, not that this process can safely inspect or mutate a
  job cgroup.
* `/proc/filesystems` exposes both `cgroup` and `cgroup2`.
* `/proc/sys/kernel/random/boot_id` is readable and provides a node boot
  identity source, subject to the trusted node integration binding it into its
  authenticated event.
* The visible `/dev` listing did not expose a GPU character device to this
  namespace. That cannot establish the node's physical GPU inventory.
* `/etc/slurm`, `slurm.conf`, `cgroup.conf`, and `gres.conf` were unavailable
  in the workspace namespace. The configured Slurm cgroup plugin, GRES device
  mapping, cgroup version, and privileged slurmd access policy are therefore
  unknown.

## Consequence

There is no locally reliable basis to claim positive GPU cleanup. The prototype
therefore requires the trusted slurmd evidence fields
`device_release_confirmed`, `cgroup_empty`, and the race/PID checks. It will
remain unknown unless a real authorized integration supplies and authenticates
those facts. No production capability or feature-complete behavior is claimed.

