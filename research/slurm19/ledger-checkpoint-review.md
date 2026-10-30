# Staged WAL sequence checkpoint

`ledger-checkpoint.patch` follows `d436228`. Each WAL event is fsynced before writing and fsyncing a separately opened, owner-checked, single-link checkpoint file containing its sequence. Startup replays the WAL and requires an exact sequence match with the checkpoint; missing, malformed, mismatched, or incomplete state fails closed. A crash between the two durable writes can require administrator recovery, rather than guessing whether an event committed. Neither file is silently created by the controller; initial provisioning and recovery require a separately reviewed procedure.

This detects a WAL-only rollback to an older complete event prefix if the checkpoint remains current. It cannot detect rollback or coordinated modification of both files, nor same-sequence content changes; the state directory and checkpoint must be protected and an external trusted anchor or checksum/integrity design is still needed for stronger guarantees. It adds one synchronous checkpoint write per event and may affect scheduling latency. Code is unvalidated for crash/power-loss behavior; controller remains disabled and uninstalled. No tests were run.

On 2026-10-30, the `1adcfef` patch applied to the isolated u20 source and `nice -n 19 make -j2 -C src` exited 0. No warning/error lines appeared in the incremental build log. Source-tree controller SHA-256: `0aa59c66267c8ddfbfc92527fe9960ee885def69f97090a17ee68ce7d68d9437`. Running `slurmctld`, `slurmd`, and `pro6000d-training` reported active afterwards. This build is not deployed or behavior-tested.

`checkpoint-parser-fix.patch` follows `1adcfef` and rejects non-digit sequence bytes, including embedded NULs, before numeric parsing; it is part of the candidate and has not been behavior-tested.

On 2026-10-30, `1fc5aa7` applied to the isolated u20 source and `gcc -std=gnu99 -I. -fsyntax-only src/slurmctld/monthly_quota.c` exited 0; the prior linked binary does not yet include this final parser change.
