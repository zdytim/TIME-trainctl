# u20 NVIDIA permission reset: trace and user-slice check

On 2026-10-31, fixture `1c7bce3` ran in a networkless `runc` container with a
private `/dev`, without NVIDIA container-runtime hooks or host device bind
mounts. It created character devices with the host's GPU, control and UVM
major/minor numbers at mode 0600, and used copied host `nvidia-smi`, NVIDIA
management library and `strace`. Container device inode `1048638:17` for GPU 0
differed from host inode `5:684`. The query returned NVIDIA GeForce RTX 4090.
The trace showed the process calling `chmod(..., 0666)` on its private
`/dev/nvidiactl`, `/dev/nvidia0` and `/dev/nvidia-uvm`; all three private nodes
then had mode 0666. It also created private NVIDIA capability nodes. The image
SHA-256 was `cd8b42de13511910f8079a40f0c1bf8dec4016c89528ef4b440cea6b057d2d84`.

This establishes that the tested root NVIDIA userspace query can widen device
permissions on nodes visible in its own mount namespace. It does not prove
which component caused the separate host ACL change during the preceding GPU
container test, or cover all driver and runtime paths. The private test did not
bind host device nodes, change kernel module options, or modify host udev rules.
Afterwards host GPU, control and UVM ACLs still showed `other::---`.

Fixture `6a039cb` then created three temporary mode-0666 nodes under a private
directory on the host `/dev`, with the same major/minor numbers as the current
GPU, control and UVM devices. Root successfully opened and closed each node as
a positive control. A transient service running as `timzhang` in the existing
`user-1001.slice` received `EPERM` for all three. Its temporary nodes and unit
were removed. This supports the user-slice boundary for that account and those
device numbers even if DAC is permissive; it does not cover non-enrolled users,
processes outside that slice, privilege escalation, dynamically added device
numbers, reboot, or a full SSH/Cockpit login.

At the read-only inventory immediately beforehand, interactive shell accounts
were root, administrator `u20`, and training user `timzhang`; this can change.
After both tests Slurm and the training backend remained active, the host queue
was empty, the three existing NVIDIA nodes had `other::---`, and Sunshine PID
`130919` remained. Monthly enforcement was not enabled. No durable fix to the
device permission reset was installed.

On 2026-10-31 after the monthly controller cutover, `a4a9a75` ran a bounded
loopback-only temporary sshd using ephemeral keys and the actual production
`sshd` PAM stack. A real `timzhang` SSH session entered the devices hierarchy
at `/user.slice/user-1001.slice`. Root opened a separate temporary mode-0666
character node with the real GPU device number as a positive control; the SSH
session received `EPERM` opening that same node. Its parent directory was
accessible only to root and the test user's private group. No production SSH
configuration, user keys or existing GPU nodes were modified; temporary device
and key directories were removed. SSH, Slurm and platform services remained
active, queue empty, and GPU other ACLs `---`.

Two earlier probe runs authenticated but failed a test assertion that required
a slash after the devices slice. The actual devices hierarchy ended exactly at
`user-1001.slice`; those runs are not counted. The corrected probe accepts the
exact slice or a child. This establishes the real SSH/PAM path for the current
account and GPU major/minor even when DAC on the temporary node is permissive;
it does not establish Cockpit, all execution paths, device major changes, reboot
or privileged-user behavior. Device mode recreation remains unresolved.

Later on 2026-10-31, a real Cockpit terminal session authenticated as
`timzhang` reported UID 1001 and devices cgroup
`/user.slice/user-1001.slice`. Opening the existing `/dev/nvidia0` for
read/write returned `EACCES`; the device was mode 0660 owned by root:root.
This confirms the Cockpit session's placement and that this direct open was
denied, but the error does not distinguish cgroup enforcement from filesystem
permissions. The earlier permissive-node `EPERM` evidence covers SSH and the
transient user-slice service, not this Cockpit session. Afterward, both Slurm
services were active and the queue was empty.

A subsequent Cockpit check used the same authenticated `timzhang` terminal
and a temporary mode-0666 character node with the current `/dev/nvidia0` device
number. Its directory was accessible to root and the user's group; root opened
the node successfully, while the Cockpit session's read/write open returned
`EPERM`. This confirms device-cgroup denial for this session and current GPU
device number even when the temporary node's DAC mode permits access. The
temporary node and directory were removed, the real node remained root:root
mode 0660, both Slurm services were active, and the queue was empty. This does
not establish protection for other users or execution paths, changed device
numbers, privileged processes, reboot, or all device recreation paths.

At 17:06 CST the live NVIDIA module reported version `570.133.07`,
`ModifyDeviceFiles: 1`, `DeviceFileUID: 0`, `DeviceFileGID: 0`, and
`DeviceFileMode: 438` (decimal 0666). The same-version [NVIDIA FAQ](https://download.nvidia.com/XFree86/Linux-x86_64/570.133.07/README/faq.html)
states that NVIDIA userspace can modify existing device nodes to its configured
mode and that `NVreg_DeviceFileMode` controls that default. This corroborates
the previously observed private-node chmod mechanism, but does not identify
the process or time of the latest host permission change. `auditd` was inactive
at inspection, so no matching audit event was established. The live device
nodes had mode 0660 with platform ACL entries after restoration.

A restrictive module default is a candidate for a later controlled change, not
a verified fix: current training and display access depend on named ACLs and
the udev helper; cold boot, UVM/capability nodes, privileged software and
in-flight training require separate review. No module option, initramfs,
driver, or service was changed during this investigation.

Further read-only journal and boot inspection identified an intervening host
boot at 2026-10-31 16:42:39 CST. The kernel loaded the NVIDIA modules at
16:42:47 and the current device nodes have a 16:42 creation timestamp. This
boot predates the 17:05 ACL restoration and postdates the earlier 16:28
inspection, so a boot/device-recreation path is a relevant possibility; it
does not establish which component widened permissions or when. NVIDIA
container-toolkit activity was also logged at 16:43. No reboot was initiated
as part of this investigation. Subsequent checks showed all three nodes with
`other::---`, Slurm and the broker active, and an empty queue.

After this intervening boot, the configured `7a06` controller was running and
the shared CLI, as `timzhang`, reported the same 1.950998992 settled GPU
seconds recorded before the boot, October 2026, tier debug, and mode enforcing.
This is evidence of a completed-job ledger surviving this boot; it is not a
test of recovery with an active training job, a forced power failure, or
device access before ACL restoration.

The boot journal places `pro6000d-gpu-acl.service` completion at 16:42:47,
Docker startup at 16:42:53, and a Sunshine container restart with NVIDIA
container-toolkit activity selecting the NVIDIA nodes at 16:43:09. This
establishes that the one-shot ACL service ran before that later NVIDIA
userspace activity; it does not prove that the toolkit changed node modes.
The service being `active (exited)` proves its earlier invocation completed,
not that subsequent device permissions are still restricted. Three main GPU
nodes remained `other::---` on the later check; Slurm and broker were active
and the queue was empty.

An attempted isolated test of a synthetic restricted `/proc/driver/nvidia/params`
was stopped by the container runtime, which forbade mounting a fixture inside
`/proc`. The test process did not start; the fixture was removed. No module
parameter or host device node was changed by the attempt. This leaves the
candidate default-mode behavior and boot ordering unverified, rather than
establishing a durable fix.

Later, the existing private-parameter simulation in
`u20-nvidia-device-mode-isolation-20261030.md` was reexamined: simulated 0660
changed the private GPU and control nodes to 0660, but the private UVM node
still became 0666; separately the vendor `ub-device-create` was observed in
an isolated fixture to set its private nodes to 0666. Therefore the NVIDIA
mode parameter by itself is not evidence of a complete fix.

A further post-reboot check used the actual `timzhang` user manager and
`systemd-run --user` to start a bounded Python process. Root created a
temporary mode-0666 character node matching the current GPU device number in
a root/user-group-only directory and successfully opened it as a positive
control. The user service reported devices cgroup
`/user.slice/user-1001.slice` and received `EPERM` when opening the duplicate.
The temporary node and directory were removed. Slurm and broker stayed active,
the queue was empty, and the three main real devices retained `other::---`.
This validates that account's tested user-manager path and current device
number, not every account, device, launch path or future boot.

Read-only `udevadm test --action=add` on the live `/sys/module/nvidia_uvm`
and `/sys/bus/pci/drivers/nvidia` paths listed `/sbin/ub-device-create`
followed by `/usr/local/libexec/pro6000d-gpu-acl` in both RUN lists. The
existing `KERNEL=="nvidia*"` rule therefore also matches these two tested
parent events, not only the character-device paths. For those events
`DEVNAME` was absent in the simulated result, so the helper would receive no
device argument and its existing code would enumerate eligible nodes. The
test does not execute RUN commands, establish cross-event ordering, or cover a
later userspace chmod that generates no matching udev event. It corrects the
earlier incomplete inference that the rule only covered device-node events;
it is not a durable-fix or actual boot verification.

At 17:4x CST a read-only follow-up found that the current `timzhang` account
is UID 1001, belongs to its own group and `pro6000d-training`, and does not
belong to the administrator group. Its systemd user slice reports
`DevicePolicy=closed`. Both SSH and Cockpit include `common-session`, whose
session guard runs after `pam_systemd`; cron is active and its account policy
rejects non-admin human accounts through the installed cron guard. The
interactive account inventory still contains only administrator `u20` and
training user `timzhang`. These are configuration observations, not a new
negative-path execution test, and do not prove protection for all services,
privileged processes or future accounts. The enrollment tool requires a
logged-out ordinary user, rejects several privileged groups and adds a
per-user closed slice; it must actually be run for each new training user.
Slurm and the platform backend remained active and the queue was empty.

At approximately 17:5x CST, inspection found `/dev/nvidia-modeset` mode 0666;
the current ACL helper intentionally excludes the display/modeset node, so the
previous three-node ACL checks do not cover it. A bounded transient systemd
service ran as UID 1001 in the existing `user-1001.slice`; its devices cgroup
was `/user.slice/user-1001.slice` and an `O_RDONLY` open of the actual
mode-0666 modeset node returned `EPERM`. A separate root read-only open
succeeded. This verifies denial for this UID, this device and this transient
slice path despite permissive DAC; it does not prove that modeset can or
cannot be used to bypass compute, cover other accounts or paths, or make the
NVIDIA device-node permissions durable. The live modeset node was not modified;
no training service was restarted. When inspected before the probe the slice
was inactive and had no live cgroup, so its configured `DevicePolicy=closed`
alone was not treated as proof of an active cgroup.

At 18:16 CST a bounded descriptor-handoff probe used a temporary mode-0666
character node under a private `/dev/time-fd-handoff-*` directory, with the
same device number as `/dev/nvidia0`; it never changed the real node's mode.
Root opened the duplicate as a positive control. A `timzhang` process in a
transient `user-1001.slice` service received `EPERM` opening that duplicate
directly. A separate same-UID process launched from root's unrestricted SSH
service context opened the duplicate and passed its file descriptor through a
Unix socket using `SCM_RIGHTS`; the restricted process received an open
character-device descriptor. The probe (`sha256:f4b3f0a1060998d6ce563bd667a04b31e21fa7c4d69a1e939141ced0caf99ac4`)
used no CUDA ioctl or computation, and the sender was a controlled surrogate,
not an actual Slurm job. Thus it establishes a file-descriptor transfer path
across the tested cgroup boundary, not successful unscheduled CUDA execution.
It strengthens the previously documented need to investigate job-to-login
FD transfer before describing the isolation as resistant to intentional
bypass. Temporary nodes and the transient service were removed; afterwards the
queue was empty, all three services were active and the three core device ACLs
still had `other::---`.

At 18:18 CST the descriptor handoff was repeated using a **real platform
Slurm job**, not the earlier unrestricted sender surrogate. After checking
that the scheduler queue and platform active-job list were empty, a bounded
one-GPU task was submitted as `timzhang` (platform ID
`5d3c8ad6949d4935a07018faa19531e8`, Slurm job 13). Its own log recorded
the devices cgroup `/slurm_u20-Z790-S-DDR4/uid_1001/job_13/step_batch`
and `TIME_FD_JOB_SENT`; it opened the real `/dev/nvidia0` and transferred the
descriptor through a Unix socket to a same-UID process in the restricted
`/user.slice/user-1001.slice`. The receiver was denied direct open with
`EPERM` against a temporary mode-0666 duplicate node of the same device number,
but received a descriptor whose `fstat` identified the real GPU device. The
probe program SHA-256 was
`2a268696a1659741f74813ac723b5dc5bbf3caf264d5f464934d4abffdf15776`;
its test files and output remain in `/home/timzhang/.time-fd-job-rru4jfzg`.
The socket, duplicate node and receiver service were removed. No CUDA ioctl or
computation was attempted outside the job: this proves transfer from a real
job, not that a complete CUDA workload works on the received descriptor.
Afterwards the scheduler queue was empty, Slurm and the backend were active,
and core GPU ACLs had `other::---`. This is a demonstrated isolation gap at
the file-descriptor boundary; an enforcement design must address it before
claiming resistance to intentional bypass.
