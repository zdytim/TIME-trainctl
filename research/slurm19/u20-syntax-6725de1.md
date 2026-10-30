# u20 isolated Slurm 19 syntax evidence

Target: u20, Slurm 19.05.5, GCC 7.5.0. Source input was the Git archive of official commit `78e9cfab783aeed0da68ea25532a3f35114d4072`, SHA-256 `1ce10e864bcbd3770f13de5b5eea4821420fcbce39fd6211426b068de98c3b67`; the candidate patch from commit `6725de1` had SHA-256 `ae6575a993c6a389a4d5796dc903ef12093d40ba6a6e6d0b8bd32d5de5fc11f7`. Both remote hashes matched before extraction into `/home/u20/.local/share/time/builds/slurm19-6725de1/source` and patch application.

For isolated syntax checks only, `config.h` and `slurm/slurm.h` were copied from the existing u20 review source `/home/u20/strict-controller-review-6IbC4Oex/source`. These generated headers have not been proven to match the installed Slurm ABI or configuration. `gcc -std=gnu99 -I. -fsyntax-only` returned zero separately for `monthly_quota.c`, `controller.c`, `job_mgr.c`, `job_scheduler.c`, `node_scheduler.c`, and `read_config.c`.

No linking, binary installation, tests, simulation, service changes or job operations were performed. This evidence establishes only that the six units parsed with these headers. It does not prove build/link compatibility, priority logic, GPU allocation atomicity, crash recovery or enforcement. The controller remains uninstalled and disabled.
