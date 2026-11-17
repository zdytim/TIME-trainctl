# pro6000d quota application clarity

- Deployed commit `71aee64` to pro6000d (`ubuntu`) on 2026-11-17 at 13:02:55 Asia/Shanghai.
- Updated only Cockpit training plugin `app.js`, `monthly-ui.js` and `index.html`: clarify the normal-tier monthly addition and separate long-term resource limits; show monthly quota in the account page; distinguish application pending/approved amounts; improve approval and waiting messages. Cache keys were updated. Controller, Slurm policy and existing time-limit fields were not changed.
- Backup: `/var/backups/pro6000d-training/ui-quota-ux-20261117T050255Z`. Rollback was not needed; restore those three files from the backup if required.
- Verification: independent code review, JavaScript syntax and Git diff checks; deployed file hashes matched and the controller, node and training management services remained active. No service or training task was restarted. Browser interaction was not directly verified.
