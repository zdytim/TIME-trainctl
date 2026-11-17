# pro6000d GPU performance display by account quota

- Commit `206aa32` deployed on pro6000d (`ubuntu`) on 2026-11-17 at 12:40:34 Asia/Shanghai.
- Changed Cockpit training plugin `app.js` and `index.html` only. The performance page limits displayed shared GPU cards, charts and process rows to the current account's GPU quota; cards with the account's observed GPU processes are preferred. This is a display selection, not a reservation or a change to scheduling or API authorization. Browser cache key was updated.
- Backup: `/var/backups/pro6000d-training/ui-gpu-quota-20261117T044034Z`. Rollback not needed; restore the two files from that directory if required.
- Verification: JavaScript syntax and Git diff checks passed; deployed files matched their committed SHA-256 hashes; slurmctld, slurmd and pro6000d-training all remained active. No service or training task was restarted. Browser rendering under another user's account was not directly verified.
