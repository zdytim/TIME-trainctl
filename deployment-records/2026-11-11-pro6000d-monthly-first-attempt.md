# pro6000d monthly quota: first activation attempt

- Target: `ubuntu` (`pro6000d`); implementation commit `91ce68bbd8adb19923e0d017551c3a8b49bd2b20`, pushed to `origin/pro6000d-slurm26`.
- 2026-11-11 15:52 CST: platform code staged with monthly controls disabled. Backup: `/var/backups/pro6000d-training/staged-upgrade-20261111T075224.353035Z`.
- 15:53 CST: activation began with an empty queue; backup: `/var/backups/pro6000d-training/monthly-release-20261111T075301.342962Z`.
- Before admission was opened, the paired services failed to start. The original slurmd unit's ordering after slurmctld conflicts with the new paired ordering; a drop-in cannot clear that existing ordering. The GPU mapping check also refused NVML's PCI order, which differed from device-minor order.
- 15:54 CST: automatic rollback restored the original Slurm installation and configuration. The old controller, node and training API were active; the GPU partition was idle and the queue was empty. The initial quota ledger and node evidence were retained for inspection. Monthly enforcement was not activated.
- An isolated target-node diagnostic confirmed that explicit GPU files with `AutoDetect=off` satisfy the device-minor check. This removes NVML-provided GPU/CPU affinity and link topology; the subsequent release must record that scheduling change and verify actual allocation and settlement.
