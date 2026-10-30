# Staged typed-GRES admission guard

The Slurm 19 `gres_get_job_state(list, "gpu")` lookup compares a typed GRES record's `type_name` (for example a model name) to `gpu`. It can therefore miss a typed GPU, and the previous quota classifier could treat it as a CPU-only job when the generic GPU TRES was also absent. The analogous lookup could miss typed GPU-like GRES as well. That is a potential fail-open path and blocks activation of the prior candidate.

The staged change adds a Slurm GRES helper that walks the job's GRES records by plugin identity under the existing GRES lock. It detects GPU records independently of type names and returns unsupported for typed GPUs, duplicate GPU records, and MPS/shard/MIG plugin records. Only a single untyped GPU record proceeds to the existing quota checks; no unsupported GPU shape is silently treated as CPU-only. Unknown GRES aliases that access GPU hardware are not automatically identified and remain an explicit configuration boundary.

This does not solve the separate u20 missing-GPU-TRES prerequisite. It is staged code, not proof that every Slurm path is covered: direct submit, typed and mixed requests, job recovery, plugin linking and pre-allocation behavior still require isolated validation. No test, live configuration change or service restart was performed.

On u20 the patch from commit `c888525` applied to the isolated incremental candidate and `make -j2 -C src` completed. The resulting controller ELF SHA-256 is `3bd813744f6a9952f4c3658bb04d47be8306554e55cb7a5cfa407999dee4c151`; existing Slurm and training services reported active afterward. The candidate was not installed or run, and successful linking does not establish runtime behavior.
