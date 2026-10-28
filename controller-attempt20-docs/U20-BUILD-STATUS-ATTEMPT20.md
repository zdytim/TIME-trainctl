# u20 build and deployment boundary

The build was performed only in a new task-owned directory with the clean official source and attempt20 patch. The first run recorded that the sandbox `/tmp` is read-only; the successful retry used a new task-owned `TMPDIR` and a new build output directory.

The isolated build compiled `src/slurmctld/.libs/slurmctld`. Configure reported unavailable PAM and munge libraries, plus other optional host integrations. This does not validate the installed u20 ABI, authentication, PAM, GRES plugin, select plugin, or TRES map. No packages were installed and no production configuration was read or changed.

Supervisor-provided u20 facts state `accounting_storage=none` and that `AccountingStorageTRES` did not include GPU at the last check. Those facts were not independently queried here. The controller must not claim GPU scheduling support until the deployed configuration proves a typed GPU TRES mapping, matching select/GRES plugins, recovery ordering after association/TRES initialization, and socket readiness only after recovery.

Installation remains staging-only. `tools/install-controller.sh` is root-only, refuses an existing version directory, checks the relative manifest against the artifact and destination bytes, and has explicit `--enable`/`--activate` flags. It was inspected but not executed. Rollback is a reviewed symlink/version procedure and was not executed. No service was restarted.
