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
