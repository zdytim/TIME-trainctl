# CPU-only array shape correction

`cpu-array-shape-fix.patch` follows `3ccd0a9`. It moves the unsupported-array check after the existing GPU/GPU-like GRES detection. A genuinely CPU-only array can therefore bypass GPU accounting instead of blocking its own admission or making the whole GPU ledger unhealthy at restart. GPU arrays and GPU-like GRES remain rejected, and other ambiguous shapes remain conservative. This relies on Slurm's restored GRES/TRES records and has not been behavior-tested; the strict gate remains disabled.
