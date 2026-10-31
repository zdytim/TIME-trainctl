# u20 controller-only configuration preview

The `cdad3c7` generator produces an in-memory copy of a known u20 Slurm
configuration with only a candidate `PluginDir` and
`SlurmctldParameters=MonthlyGpuQuota` appended. It rejects included files,
existing values for either option, conflicting or absent expected baseline
settings, unsupported escapes, oversized input and invalid UTF-8. It never
writes a config or installs/enables the candidate. Four local fixture tests
passed for baseline preservation and refusal cases.

On u20 at 2026-10-31 approximately 12:10 Asia/Shanghai, the verified script
read `/etc/slurm-llnl/slurm.conf` without modifying it. Its baseline SHA-256
was `6c6072b71804357a06c270f883981f95b9b2ae4ce35913026b42956d69172c63`;
the in-memory candidate SHA-256 was
`8ce457b2deec7c42438a06db2360d01163e681dea351d05a68bdacc8ab658411`.
The temporary script copy was removed. The queue was empty, controller, node
daemon and backend remained active, and live `SlurmctldParameters` was unset.

This is a narrow preview, not a full Slurm syntax validator or an install
procedure. Recheck the live baseline at cutover: a stored hash is not a lock.
Any generated file and its parent require trusted ownership and atomic update;
the controller launcher, configuration drift, node registration, state and
database backups, workload protection, and rollback must still be addressed.
Monthly enforcement remains disabled.
