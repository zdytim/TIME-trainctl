# Staged selected and allocated GPU bitmap checks

Slurm 19's cons_tres `_add_job_to_res` does not check the return from `gres_plugin_job_alloc`, and the latter writes a per-node count while processing allocation. A count by itself therefore does not independently establish that the corresponding device bitmap exists and contains that many devices. On u20 the configured GPU GRES has a `File=/dev/nvidia0` device.

The staged no-TRES, single-node fallback now also requires the selected device bitmap to exist and match the selected count before admission, and the allocated bitmap to exist and match the allocation count after allocation/recovery. Mismatch after allocation is treated by the existing controller as ambiguous and closes future admission; it does not undo a partial Slurm allocation or terminate an existing job. These checks do not prove exclusive access, successful device opening, or that every selector failure propagates.

Abstract GRES without device bitmaps and test configurations that cannot provide them will fail closed. This is a candidate for file-backed GPUs only; real device behavior and a suitable isolated validation setup remain unproven. No test, service restart or deployment was performed.

The patch from `e044f40` applied and compiled in the u20 isolated candidate; the controller ELF SHA-256 is `b5638f30db691232a95790335ab14bf32f29d8d3cc830cec4f4c604e7f6a979c`. Existing Slurm and training services reported active after compilation. The candidate was not installed or run.
