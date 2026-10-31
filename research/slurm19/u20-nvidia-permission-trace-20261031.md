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
