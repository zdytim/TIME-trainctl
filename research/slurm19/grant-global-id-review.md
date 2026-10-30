# Globally bound monthly grant IDs

`grant-global-id-fix.patch` follows `1fc5aa7`. A grant ID is now unique across the controller ledger, not merely per user/month. Replays or repeated calls match only when UID, month, amount and actor all agree; changing any of those fails instead of applying a second grant. UID narrowing and month range are also checked before a grant. Existing incompatible historical entries would fail closed on replay and require review; no existing production ledger was altered.

This is only the controller-side primitive. There is still no authenticated grant transport, broker-to-controller acknowledgement, or runtime validation. No tests were run and the gate remains disabled.

On 2026-10-30, `c97852d` applied to the isolated u20 source and `nice -n 19 make -j2 -C src` exited 0, with no warning/error lines in the incremental log. Source-tree controller SHA-256: `da517db57b4c640060cad6e7c8f4f385ae672c8b9b4ffdaa1a5834c7bed62ab2`. The running controller was not replaced; the three Slurm/training services reported active after the build.
