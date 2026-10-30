# Slurm controller link repair

An isolated u20 `make -j2 -C src` build failed at slurmctld link with missing node-management symbols. The original staged patch accidentally replaced `node_mgr.o` with `monthly_quota.o` in the generated `Makefile.in` object list even though `node_mgr.c` remained in sources. This incremental patch restores `node_mgr.o` while retaining `monthly_quota.o`. The `Makefile.am` source list already contains both source files.

This is a build fix only. A successful link would not establish binary ABI compatibility with the installed package or prove the controller's quota behavior; the candidate remains disabled and uninstalled. No tests or jobs were run.
