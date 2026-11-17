# pro6000d strict-priority release

- Deployed commit: `02c32d5692b14810c49e8eddd9ca9cf847622402` on pro6000d (`ubuntu`).
- Completed: 2026-11-17 11:48:23 Asia/Shanghai (03:48:23 UTC).
- Components: Slurm controller and node daemons, scheduler plugins, management backend, Cockpit UI, and management service priority-storage drop-in.
- Backup and journal: `/var/backups/pro6000d-training/priority-upgrade-20261117T034802.986966Z`. The original live WAL, checkpoint, witness, node proof and database were preserved. Candidate tree SHA-256: `d96609bc002a3aeb6ad5c2eaca16f6e35cb152e605977411f28aaca926b48d1c`.
- Verification: the quiescent cutover rechecked an empty scheduler, no open platform holds, and idle GPUs; existing monthly release and candidate source/artifacts passed verification. The post-start API reported priority available and reconciled, monthly enforcement ready and selectable tiers ready. An independent post-cutover check confirmed all three services active, `strict_priority_enabled` true, the priority release symlink in use, and GPU partition UP. No user task was interrupted.
- Rollback: not attempted after admission was reopened. Preserve current ledgers; the previous controller cannot read new RELEASE3 records, so do not replace the live ledger with the backup. The preceding failed fenced attempt and successful restoration are recorded separately in `2026-11-17-pro6000d-priority-attempt.md`.

The isolated fake-GPU scenarios and narrower recovery checks are described in `research/pro6000d/20261116-private-selector-evidence.md`. Complete cancellation-to-clear recovery and cross-file WAL/checkpoint/witness crash repair were not validated; ambiguous ledger divergence remains fail-closed and may require operator intervention.
