# u20 Slurm19 review package

The patch targets only the verified Slurm 19.05.5 source. It includes a durable
ledger, compatibility API/frontend materials, and source integration points,
but strict enforcement is disabled because allocation linearization, bounded
scheduler I/O, and the production GPU TRES mapping are unproven. Read
`COVERAGE-AND-LIMITATIONS.md` and `AUDIT-FIXES.md` before any future review.
