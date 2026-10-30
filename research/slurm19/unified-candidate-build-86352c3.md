# Unified u20 candidate build: 86352c3

On 2026-10-30 13:18 Asia/Shanghai, revision `86352c3521b7d00ba615a126dadec0a176c42943` was cloned from a committed Git bundle into `/home/u20/.local/share/time/builds/repo-86352c3`. Its CLI/MCP 16-entry manifest matched and top-level Python files parsed. The same revision assembled and parsed the broker candidate in `repro-broker-86352c3`, SHA-256 `c4b1649badedd5c0c063a1152f33ab7625ff173f7102980dcd89dac7a16201c5`.

The pinned Slurm 19 archive and all 31 project patches reconstructed and compiled in `repro-slurm-86352c3`. The controller ELF SHA-256 is `e50f4a86da9e78a3954356cf472c65c210857f533be155922852c774d457fbdb`; the backfill plugin SHA-256 is `ad2ac4e997fcb7f524b07b728d02630a1aaf80233567e53e684b5ad2981afa4d`. At the subsequent read-only observation, slurmctld, slurmd and training service were active, the queue was empty, and `/home` had approximately 26 GiB available. Binary hashes depend on build paths.

No candidate or test was run, no existing wrapper or service was changed, and quota enforcement was not enabled. Build success does not establish correct GPU allocation, controller/plugin loading, scheduling, accounting, recovery or safe deployment. Existing candidate directories and backups were preserved.
