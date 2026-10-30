# Unified u20 candidate build: d17659a

At 2026-10-30 13:08 Asia/Shanghai, project revision `d17659aa9a40e02dc9cb386e88f234b934533cfe` was independently cloned from a Git bundle into `/home/u20/.local/share/time/builds/repo-d17659a`. The committed 16-entry CLI/MCP manifest matched and the top-level Python files passed AST parsing. The same revision assembled the broker candidate into `/home/u20/.local/share/time/builds/repro-broker-d17659a`, SHA-256 `aa9399ec29c7b2a615cf1bd08a2c75e5d59df50b359d91bfab62705cbfecd2e2`.

The pinned Slurm source and all 30 committed patches reconstructed and compiled successfully in `/home/u20/.local/share/time/builds/repro-slurm-d17659a`. The controller ELF SHA-256 is `300447f68b0913d9b52bb77ea0c68215296365f8e4cecf25a543af3bb89d8dbd`, and the backfill plugin SHA-256 is `68c19f53c0ae551d3036b4e809ff26e6c7b55acc6b737453172080cd239cc2f7`. Binary hashes are path-dependent. Existing slurmctld, slurmd and training services reported active and the queue was empty at the subsequent observation.

This is source reconstruction, compilation and static artifact verification only: no candidate process or plugin was run, no test or workload was started, and no service or installed wrapper was changed. It does not establish GPU count correctness, quota admission, plugin ABI compatibility, crash safety or production readiness. The monthly gate remains unenabled.
