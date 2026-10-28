# Minimum u20 build, install, and rollback procedure

The operator must first verify the installed controller reports the exact
19.05.5 ABI and obtain the corresponding configure arguments and `slurm.conf`
from the u20 host without changing services. Those production facts were not
available to this task. The available u20 facts state
`accounting_storage=none` and no GPU in `AccountingStorageTRES`; the patch
therefore refuses strict activation until the real host mapping is reviewed.

The isolated build requires a new task-owned output directory, a clean git
checkout of `slurm-19-05-5-1`, GCC, GNU make, a POSIX shell, binutils, libc
headers, pthread and dynamic-loader development support, and the source tree's
existing configure/libtool machinery. Optional runtime/build integrations must
match the host: munge is needed when the site uses `AuthType=auth/munge`, PAM
headers/libraries when PAM is enabled, and the site GPU/select/plugin libraries
must match its Slurm build. This sandbox lacked PAM and munge; the resulting
binary is not a drop-in u20 binary until the operator rebuilds with the verified
u20 options and dependencies.

1. In a new empty build directory, fetch or unpack the verified source and
   confirm the exact tag and commit.
2. Inspect `tools/build-controller.sh`; it refuses any pre-existing output
   directory. Run it with the exact operator-supplied configure arguments:

   ```sh
   tools/build-controller.sh \
     --source-dir /new/path/slurm-19-05-5-1 \
     --output-dir /new/empty/u20-build-output \
     --configure-args '...verified u20 configure options...'
   ```

   The resulting `artifact/BUILD-MANIFEST` and `SHA256SUMS` must be reviewed
   against the u20 ABI and plugin configuration.
3. Stage the reviewed artifact with the actual `SlurmUser` as state owner. The
   installer rejects state-directory symlinks, applies mode 0700 to the state
   directory, applies mode 0600 to the marker, and never restarts a service:

   ```sh
   tools/install-controller.sh \
     --artifact /new/empty/u20-build-output/artifact \
     --state-owner SlurmUser:SlurmGroup
   ```

4. Do not enable this revision. It intentionally rejects explicit enablement
   because the Slurm19 allocation linearization and bounded scheduler-I/O
   proofs are incomplete. A future reviewed revision must first establish the
   GPU TRES map, then arrange the Slurmctld service environment to contain
   `SLURM_MONTHLY_QUOTA_ENABLE=1`; the installer does not modify service
   configuration. Without both the environment switch and marker the
   controller is disabled. With the switch but missing state or marker,
   initialization fails closed:

   ```sh
   tools/install-controller.sh \
     --artifact /new/empty/u20-build-output/artifact \
     --state-owner SlurmUser:SlurmGroup \
     --enable
   ```

   Do not pass `--activate`; this package has no safe activation path.
5. To roll back, select an already installed prior version with the same
   durable ledger format and retain the ledger. The selector changes only the
   version symlink and does not restart a service:

   ```sh
   tools/rollback-controller.sh --to 19.05.5-u20-monthly.1
   ```

   If enforcement must be disabled during rollback, remove the service
   environment switch and marker through the separately reviewed maintenance
   procedure; do not delete the ledger. A prior binary must understand the
   retained ledger format before it is selected.

The backend remains disabled by default. Its existing API schema and the u20
example config are compatibility material; they do not grant allocation
permission. In this revision the controller socket is never exposed because
strict activation is refused. No GPU scheduling functionality or production
readiness is claimed.
