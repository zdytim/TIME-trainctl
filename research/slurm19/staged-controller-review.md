# u20 Slurm 19 controller candidate

`staged-controller.patch` is generated from the interrupted u20 draft against official Slurm 19.05.5 commit `78e9cfab783aeed0da68ea25532a3f35114d4072`. It includes changes to seven existing controller/build files and adds `monthly_quota.c` and `monthly_quota.h`; it incorporates the local clock and suspended GPU accounting corrections. A static `git apply --check` against that exact clean source succeeded.

This is a disabled candidate, not an accepted scheduler implementation. It does not prove allocator atomicity, recovery after ambiguous side effects, all job shapes or backend grant integration. The code currently maintains a combined 605 GPU-hour limit rather than an implemented dynamic debug/important/normal scheduling priority. Suspended-accounting changes intentionally reject older SUSPEND/RESUME draft WAL records instead of silently miscounting them. Deployment and active jobs are untouched.

Next work must establish target-specific header/build compatibility and inspect all recovery and scheduler paths; user has not authorized running tests. Never enable the gate or claim monthly enforcement from this patch or static checks alone.
