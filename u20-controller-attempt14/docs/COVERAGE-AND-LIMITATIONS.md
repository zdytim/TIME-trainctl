# Coverage and limitations

The source patch contains the 19.05.5 integration points for preview, main
scheduler admission, abort/release cleanup, actual-TRES commit, recovery, and
the root-authenticated grant/snapshot API. The integration is deliberately
inactive. `MQ_STRICT_INTEGRATION_PROVEN` is false, so an explicit
`SLURM_MONTHLY_QUOTA_ENABLE=1` fails controller initialization with a clear
error. The default path does not inspect or create monthly state.

The reason is a strictness blocker in the real Slurm19 flow. `monthly_quota_admit`
records an intent before `bb_g_job_begin` and `select_g_job_begin`; it releases
`mq_lock` before those resource-side effects and before
`job_set_alloc_tres` exposes the actual GPU count. The current ABI does not
provide one proven hook that atomically serializes the quota decision with the
actual resource commit and provides a complete rollback for every scheduler,
backfill, immediate, requeue, burst-buffer, and select-plugin path. A later
patch must add that proof or keep enforcement disabled. Existing running jobs
would continue and accrue overage; this package never kills or preempts them.

The ledger repairs are fail-closed and reviewable. The journal writer and
replay parser have symmetric event-plus-ten-field records. Beijing UTC+8
month boundaries are normalized before splitting charges. A committed journal
record missing from restored active jobs makes recovery ambiguous. A commit with
zero or `UINT64_MAX` actual GPU count makes the enabled ledger unhealthy and
ambiguous; ordinary CPU jobs are filtered before that call. Replayed commit
records replace intent GPU count with the recorded actual count.

Storage initialization now distinguishes first creation from loss. A new
journal and `ledger.initialized` marker are created with `O_EXCL`, each inode is
fsynced, and the state directory is fsynced after each directory entry. An
existing initialization marker with a missing journal fails closed. Existing
preliminary journals are preserved and gain the marker only after successful
replay. A missing clock fence is recovered only from a valid journal replay or
empty first initialization; replayed WAL timestamps are compared with the
current clock before repair. Clock samples take realtime and monotonic readings
under the clock mutex, while preview and priority samples are non-persistent.

API readiness is also gated. The socket is not created in initialization; it
can only be started after `read_slurm_conf` restores jobs, `assoc_mgr_init` has
established the TRES map, reconciliation has checked all active jobs, and the
ledger is healthy and unambiguous. Missing GPU TRES mapping fails closed. The
available u20 facts state `accounting_storage=none` and no GPU in
`AccountingStorageTRES`; this task did not access production to verify or alter
those facts. Consequently no GPU scheduling functionality is claimed.

The remaining I/O design blocker is explicit. Allocation WAL append, fsync,
clock-fence update, and checkpoint are synchronous and may run under the
controller mutex; no bounded latency or scheduler-lock proof was established.
Removing persistence would weaken restart and clock-regression safety, so the
feature remains disabled pending a design with bounded commit I/O and defined
crash semantics.

Supported intent is ordinary local GPU jobs only once the above proof exists.
Arrays, array parents, resize, suspend/resume admission, heterogeneous jobs,
federation, unsupported burst-buffer/select paths, and any path outside the
audited 19 `select_nodes` flow must remain fail-closed for new GPU allocations.
No SPANK, HTTP, sacct, cached API ALLOW, or frontend decision substitutes for
the controller.

No behavioral tests, simulated scheduler runs, production configuration checks,
service restarts, training, or deployment were authorized or performed. The
isolated compile and static checks do not establish production readiness.
