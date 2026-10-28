# Build evidence

Evidence was collected in the task-owned tree
`work/u20-19-patch-build-20261028/slurm-19-05-5-1`.

- The supplied source archive SHA-256, exact tag, and commit are recorded in
  `SOURCE-ORIGIN.md`.
- A fresh clean copy accepted the current patch with `git apply --check` and
  `git apply`; the applied tree passed `git diff --check`.
- The first configure attempt failed before probing dependencies because the
  sandbox `/tmp` is read-only. A second configure using a new task-owned
  `TMPDIR` completed with exit 0.
- The isolated configure reported unavailable PAM libraries, munge, NVML
  runtime, PMIx, and other optional components. The u20 production configure,
  installed plugin set, and `slurm.conf` were not available, so this is not
  deployment ABI evidence.
- `make -C src/slurmctld -j2 slurmctld` completed with exit 0 after the
  attempt8 audit fixes. The final make log contains no compiler warning or
  error lines.
- Patch SHA-256:
  `2d02c40983e5d532fa0a481b690f992c574f048d09e7e4babc4e670672015d50`.
- The exported evidence is the actual libtool-built ELF at `src/slurmctld/.libs/slurmctld`; the top-level `slurmctld` file is only a libtool launcher.
- Compiled binary SHA-256:
  `3f02edf01ec944dc8e9b82033f5b7e573fd6a5a7d2d1180c5d7d5443bacd9121`.
- Shell syntax, Python bytecode compilation, and JSON parsing checks completed
  with exit 0.

No execution tests, scheduler simulations, training, daemon restarts, service
changes, or production configuration checks were run. The successful compile
does not prove strict allocation safety, bounded scheduler I/O, or a working
u20 GPU TRES mapping; explicit activation is blocked in the source.
