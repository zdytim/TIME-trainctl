# Build, staging, and rollback instructions

These are review instructions only. They were not executed for installation or activation.

Use a new empty task-owned directory for every build and set `TMPDIR` to a new writable task-owned directory when the host temporary directory is unavailable:

```sh
TMPDIR=/new/task-owned/tmp \
outputs/strict-monthly-quota-19.05.5-u20-attempt20/tools/build-controller.sh \
  --source-dir /reviewed/clean/slurm-19-05-5-1 \
  --output-dir /new/task-owned/build-output
```

The script requires the exact tag and commit, rejects a dirty checkout, rejects an existing output directory, copies the clean source, applies the reviewed patch, configures, and builds only `src/slurmctld`. It does not install dependencies. Configure must be rerun on the target with its real PAM, munge, select/GRES, and TRES dependencies; the isolated compile cannot establish that ABI.

The artifact manifest is bound to the reviewed patch and binary. The installer is root-only and was not run:

```sh
outputs/strict-monthly-quota-19.05.5-u20-attempt20/tools/install-controller.sh \
  --artifact /new/task-owned/build-output/artifact
```

Do not pass `--enable` or `--activate` from this package. The strict gate remains disabled and explicit initialization rejects it. Any future deployment requires supervisor review, a Git commit, exact target configuration validation, behavioral validation, and a maintenance window.

For rollback, select the prior reviewed version through the supervisor’s versioned symlink procedure and verify its manifest and destination hashes. The supplied rollback script is staging documentation only; it was not executed and no service was restarted.
