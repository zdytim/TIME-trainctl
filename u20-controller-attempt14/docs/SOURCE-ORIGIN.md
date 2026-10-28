# Source identity

The supplied archive was read from the task-owned work area and verified before
extraction:

- archive SHA-256: `fb9fde9d557915c10ef153755eb834352cce727686829590644c561bf62f2018`
- exact tag: `slurm-19-05-5-1`
- exact commit: `78e9cfab783aeed0da68ea25532a3f35114d4072`
- source tree: `work/u20-slurm-19-verified-20261028/time-slurm-19-source`
- clean-tree check: passed before copying; the baseline was not modified

The patch was generated from a separate applied copy and applied with
`git apply --check` to another fresh copy of that same tag. Official source
references: [SchedMD Slurm 19.05.5 node scheduler](https://raw.githubusercontent.com/SchedMD/slurm/slurm-19-05-5-1/src/slurmctld/node_scheduler.c), [job scheduler](https://raw.githubusercontent.com/SchedMD/slurm/slurm-19-05-5-1/src/slurmctld/job_scheduler.c), and [job manager](https://raw.githubusercontent.com/SchedMD/slurm/slurm-19-05-5-1/src/slurmctld/job_mgr.c).

The u20 installed configure arguments and `slurm.conf` were not available in
permitted read-only facts. The compile evidence therefore uses isolated prefix,
sysconfdir, and localstatedir values and does not validate the production ABI
configuration or plugin set.
