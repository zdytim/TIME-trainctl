# Slurm controller link repair

An isolated u20 `make -j2 -C src` build failed at slurmctld link with missing node-management symbols. The original staged patch accidentally replaced `node_mgr.o` with `monthly_quota.o` in the generated `Makefile.in` object list even though `node_mgr.c` remained in sources. This incremental patch restores `node_mgr.o` while retaining `monthly_quota.o`. The `Makefile.am` source list already contains both source files.

This is a build fix only. A successful link would not establish binary ABI compatibility with the installed package or prove the controller's quota behavior; the candidate remains disabled and uninstalled. No tests or jobs were run.

On 2026-10-30, after applying committed patch `6d9ae4e`, u20 `./config.status src/slurmctld/Makefile` and `nice -n 19 make -j2 -C src` exited 0. Isolated `src/slurmctld/.libs/slurmctld` SHA-256: `a7d94e6a08f0784a8afdb7a96cd646328d80eac3fc0dc6f41228d0ecbbd607ef`. The uninstalled binary reports `libslurmfull.so => not found` under plain `ldd` because its private library was not installed to its configured prefix; it is not a deployable standalone binary. Build was run on the user-owned source tree and did not run a test suite. At the subsequent read-only check `slurmctld`, `slurmd`, and `pro6000d-training` reported active and `squeue` was empty; these are point-in-time observations.
