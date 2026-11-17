# pro6000d task queue GPU display

- Deployed commit `1aff2c3` to pro6000d (`ubuntu`) at 2026-11-17 12:45:15 Asia/Shanghai.
- Updated only the Cockpit training plugin's `app.js` and `index.html`. The task queue and performance page now use the same account-quota-based shared GPU display selection; the browser cache key was updated. This does not reserve devices or alter scheduling or API permissions.
- Backup: `/var/backups/pro6000d-training/ui-queue-gpu-20261117T044515Z`. Rollback not needed; the two files can be restored from that directory if required.
- Verification: JavaScript syntax and diff checks passed; source and deployed SHA-256 hashes matched; slurmctld, slurmd and pro6000d-training remained active. No service was restarted. Browser rendering under the requesting account was not directly verified.
