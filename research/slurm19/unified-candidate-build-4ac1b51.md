# Unified u20 candidate build: 4ac1b51

At 2026-10-30 12:23 Asia/Shanghai, project revision `4ac1b51bf2dce735dba6333ab370a92ce49515f9` was cloned from a Git bundle into `/home/u20/.local/share/time/builds/repo-4ac1b51`. Its 16 listed CLI/MCP artifact hashes matched the committed manifest and its top-level Python files passed AST parsing. They are staged source files, not installed client wrappers.

The same revision assembled the captured u20 broker baseline and its patch stack into `/home/u20/.local/share/time/builds/repro-broker-4ac1b51`; the broker candidate SHA-256 is `aa9399ec29c7b2a615cf1bd08a2c75e5d59df50b359d91bfab62705cbfecd2e2`. It was not executed or connected to a database or controller.

The same revision reconstructed the pinned Slurm 19 source and all 23 candidate patches and completed `--build` in `/home/u20/.local/share/time/builds/repro-slurm-4ac1b51`. The ELF controller SHA-256 is `d94896fd1d712898d9386bf41f23219a8b37dcc72f199ca33ed550eef5ddc1a8`; the backfill and builtin plugin hashes are `2df5c08eb139eb2ddad29e542760f36ac4c4349211ab7e26001dba928536e8cd` and `7f1d40bc0caafe5c23d69c9f79ffda39b3b521a57106604f8b96da4e7513b9d7`. Build paths influence binary hashes. This is not evidence of plugin loading, quota enforcement, or integration behavior.

The existing slurmctld, slurmd and training service were active and the queue was empty at the post-build read-only observation. No candidate was installed, no live service was restarted, and no test or workload was run. The monthly gate is not enabled. A later release must record its actual deployed commit and components separately.
