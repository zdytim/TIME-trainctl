# Reproducible isolated u20 controller candidate

`u20-training-platform/tools/build-slurm19-candidate.py` records the ordered controller patch stack against the pinned official Slurm 19.05.5 Git archive SHA-256. It refuses a pre-existing output directory, uncommitted/changed patches, source archive links and special members. With `--build` it configures and compiles only in the new user-owned output directory; it does not install into the system or run tests. The output is a candidate and does not establish ABI compatibility, runtime correctness or activated monthly enforcement.

On u20 the known archive is `/home/u20/.local/share/time/builds/slurm19-6725de1/source.tar.gz`; use a new path under a user-owned build directory. This script itself has not yet reconstructed or built the stack. The patches and sources must be reviewed and the full behavior separately validated before deployment.
