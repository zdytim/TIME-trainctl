# pro6000d interim UI deployment

- Target: pro6000d, hostname ubuntu; deployed 2026-11-03 10:55:36 +08:00.
- Deployed commit: `e8bff7e` on `pro6000d-slurm26`; preceding baseline commit `8e44023`.
- Component: Cockpit training plugin `app.js` only. The text clarifies existing submission, queue and per-job time tiers, and states monthly selection and GPU-hour limits are not enabled. No broker, Slurm, credentials or database change.
- Target backup: `/var/backups/pro6000d-training/ui-e8bff7e-20261103/app.js`, root-restricted; original SHA-256 `3a53888380268027526c111bc43a6e295704cf4c22385ad23b893f6ef5491717`.
- Installed SHA-256 `d1adea6224f7bbe54a5f9bd5eb5e1d41ce9560108505ec5f416fd212272072d9`; production baseline and staged file were verified before atomic replacement.
- Verification: `node --check` on staged JS passed; post-deployment hash matched; Cockpit socket and training service active; job 73 remained RUNNING with 3 GPUs. No user workflow or browser display was verified; no test jobs or service restart.
- Rollback: not required. Restore the root-protected backup to the plugin path, preserving root ownership and mode 0644; check service health and deployed hash.
- Scope: ordinary submission remains active as explicitly selected by the user. Monthly tier selection, billing, automatic rollover and exhaustion gating are not enabled by this release.
