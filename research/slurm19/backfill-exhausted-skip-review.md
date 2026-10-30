# Staged backfill handling for exhausted users

Slurm 19 backfill uses `select_g_job_test(..., SELECT_MODE_WILL_RUN)` directly when constructing its future resource map. That bypasses the `select_nodes()` quota check; the earlier code comment suggesting that check covered all backfill reservations was too broad. These reservations are scheduler predictions, not evidence that a GPU device was allocated, but they can incorrectly impede other queued work.

After the existing queue freshness check, the staged backfill plugin skips records whose snapshot tier is exhausted before future-resource prediction. Normal allocation still relies on the separate final quota gate. Tier changes after the freshness check and unsupported typed or GPU-like GRES remain separate issues; this patch does not prove that every future prediction or actual allocation respects the quota. Existing jobs are not stopped. No test or deployment was performed.

The committed patch `d687d22` applied to the u20 incremental candidate and `make -j2 -C src` completed. The backfill plugin ELF SHA-256 is `59dd42be8f48f62f01220efa1076aba1568b77a70433a7ea6d5c2748572b6bf3`; existing Slurm and training services reported active afterward. The plugin was not installed or loaded.
