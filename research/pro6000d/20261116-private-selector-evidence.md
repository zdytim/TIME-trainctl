# Private fake-GPU selector rollback evidence

On 2026-11-16 at approximately 12:34 China time, the isolated Colima fixture
ran `TIME_TEST_SELECTOR_ROLLBACK=1` and `TIME_TEST_SELECTOR_PARTIAL=1` against
a fresh ARM64 Slurm 26.05.5 test build. The test used a private PID, network,
mount and `/proc` view, a read-only cgroup mount, a systemd scope with 1200 MiB
memory maximum and low CPU/I/O priority, a private MariaDB data directory and
socket, random Slurm ports, and fake GPU devices `/dev/null` and `/dev/zero`.
The launcher checked daemon executables, scope membership, listener ownership,
loaded-path constraints and private stepd path. The fixture reported:

* `PASS: selector audit accepts the live allocation and rejects an unknown job`
* `PASS: selector failure restores GPU while existing task continues`

The daemon log contains `test-only injected selector failure after GRES
mutation`. The fixture asserted the rejected test job remained pending without
nodes, one fake GPU remained allocated to the other running test job, and the
selector audit accepted that other job. This is evidence for that synthetic
failure path only; it is not a real-GPU test, a complete crash/recovery proof,
or validation of BIND2, priority-user release, or the current platform candidate.

The source archive SHA-256 was
`f04ab90d3bad32d0e0af1442f39782e01fe6805396f6fe75fc488d157e5f7ae2`.
The test build used `build-slurm26-isolated.sh` with its explicitly defined
allocation guard, lifecycle, transaction, selectable-tier, SLUID, tombstone,
priority and test-fixture macros. The controller binary SHA-256 was
`9e46dac579afc64b5a3b0d4e4a947548b3f7f9ccfbb343fe49f0e2ea7fed9868`;
`select_cons_tres.so` was
`6fa80ac253f7446fa5f2f86682e762803c946e90beeafcaa11dc5fc49358846d`.
The tested fixture script SHA-256 was
`5e109a594cc47a40ad5dcddc3ec72653da3bddf342fa35e772ccb4cccf290c38`.

Evidence remains in the Colima VM under
`/tmp/time26-gpu-probe-b4bb9967fc2e42adafece442341d3a1d` and
`/tmp/time-slurm26-private-KRL00lD8`. The private database was stopped after
the test. The 18 pre-existing Colima Slurm process IDs observed before the
run were still present afterwards. No pro6000d service, GPU, or user job was
modified. No production deployment is implied.

### Silent selector mutation scenario

On 2026-11-16 Asia/Shanghai, the same isolated build was run with the private launcher's `silent` mode. The first attempt stopped during fixture setup because `scontrol` reported RUNNING before its batch stepd was visible; no rollback assertion was reached. The fixture was changed to wait up to 15 seconds for the private stepd, then a new independent run completed with exit 0. Its private test evidence is `/tmp/time26-gpu-probe-5452c5ef3b6d41dd94079b70e3436152` and private install is `/tmp/time-slurm26-private-aOTTf5Qg` in Colima. The fixture passed `TIME26_TEST_SELECTOR_SILENT=1` to the private controller; the daemon logged the test-only injected selector failure after GRES mutation. Assertions observed the new job PENDING with no nodes, one GPU allocated to the already running fake job, and the existing job still RUNNING with its selector audit passing. This does not establish a real GPU, crash recovery, production behavior, or priority/BIND2 integration. The launcher shares host kernel, filesystem, I/O and Munge despite namespaces and resource constraints.

SHA-256 for this run: fixture `e5e96e9c206be7d81e1fcab630772880d83d27b10245fbb9c084c2f7c1e5a337`; launcher `924e8ebc10102f8242167a2abc9bca8dfeaa077417866393c016a08ad28318b5`; private runner `7c6c69c0e3bcc34aa545423a3940cc6bc6dda7942c26e2d41dfbce11810004c7`; private install hash-manifest `7bfa825c64d59ecaee69c43c5d86921fe3f8c777fc92718ad61993c92b780a6f`. No production service or GPU was used.

### Running job, controller restart, and settlement

On 2026-11-16 Asia/Shanghai, the same experimental build was exercised using the new private `restart-running` mode. Its independent Colima evidence is `/tmp/time26-gpu-probe-bae5c9602a164b13889da648d621b4e8`, with private install `/tmp/time-slurm26-private-vFNSqyCK`. Exit 0 reported that completion metadata and node cleanup proof preceded release, and that the exhausted job remained unallocated while the eligible job settled and its ledger recovered after controller restart. The fixture also compared the active reservation fields across the first restart, checked the same task PID remained present, verified the restarted private controller's executable, maps, cgroup and listener, waited for natural completion, and compared the released reservation and most snapshot fields across the second restart. The fixture checks nonzero consumption and event order, not exact billing amounts; PID reuse is not excluded by the PID check. The 18 pre-existing stepd PIDs remained listed after the run, which does not prove zero shared-resource impact. This is a graceful process restart using fake GPU devices; it does not prove power-loss recovery, real GPU operation, priority or BIND2 integration, or production deployment.

SHA-256: fixture `076742eab920d8591aeb1133aab4c3b3ac990f84102ef924a0921f7e1f22c344`; launcher `9e9a242a4413167a3b51bf0e1f27b50d6cfb7d990ffd6e61970322464d08be13`; private runner `57a71c8bab234352cd69b4fb88461fb69609857735b26f04a52374d6706d1f2b`; private install hash-manifest `f49c15e110fa1405fdb8403b15e1118fcef3af1d5221746062fba3e453a6216f`.

A second `restart-running` run passed on 2026-11-16 with evidence `/tmp/time26-gpu-probe-e40c3a579de745a4823e96cc56896bef` and private install `/tmp/time-slurm26-private-Wh2GKn47`. In addition to the earlier checks, the fixture compared Linux process start time across the first restart and asserted exactly one each of the tested job's GPU_MAP1, COMMIT5, ENDING2, SELECT_DONE1 and RELEASE2 events. Fixture SHA-256: `2902539a096f530b43635770bce7a8526b0e27f0c16ac4bfe40b7ad7613f399b`; private install hash-manifest: `851faca8557a2c575ea6acc1dc0a48f1444f5212e80915e78db59b070143df79`. Other scope and billing limitations above still apply.

## 2026-11-17 strict priority isolated evidence

In a private Colima PID, mount and network namespace, the candidate controller and broker used a fake two-GPU inventory, a disposable MariaDB and a fixture-only release-attestation substitute. An ordinary user's task occupied one simulated GPU. The administrator promoted a different user while that task was running; the promoted user's two-GPU task remained pending. A subsequent ordinary one-GPU task did not obtain a GPU during the observed wait, despite one simulated GPU remaining free. The fixture reported PASS and exited successfully. Its retained daemon and ledger evidence is under `/tmp/time26-gpu-probe-13e2af183afd4d3eadb73b84a7893e8f` in Colima. This does not test production attestation, a real GPU, already-submitted pending-task conversion, crash recovery, or production deployment.

A separate private smoke exercised administrator promotion, automatic start of a top-priority task and its completion with a `RELEASE3` ledger record. Its retained evidence is `/tmp/time26-gpu-probe-717a2ee8dc2648259a57cb531e52f081` in Colima; it used the same fixture-only attestation substitute. A release build on pro6000d was staged under `/tmp/time26-candidate-K5IjjimR` and is an earlier candidate, not the final release artifact.

The existing production monthly release was checked read-only on 2026-11-16: its controller reported ready; its WAL had five committed allocations and five matching release records, and its platform database had only terminal jobs and closed holds at that moment. Those observations are snapshots and must be repeated under the deployment fence. The existing first-activation script must not be reused: it initializes a new ledger and resets usage. The new `deploy/upgrade-strict-priority.py` is a draft with an explicit hard stop; it has not deployed anything. Pending-job promotion/demotion and cross-file crash recovery remain incomplete, and the candidate controller's new `RELEASE3` record cannot be read by the previous controller. Do not roll back by replacing the live ledger with its old backup.

A further disposable two-GPU fixture exercised promotion of an already-submitted ordinary Slurm PENDING job: another user's job held one GPU, the pending job requested both GPUs and retained its original scheduler ID on promotion. The v2 promotion table had no active batches and one completed receipt after promotion; the promoted job remained PENDING with a bound priority marker, and a later ordinary task remained WAITING_QUOTA with no scheduler allocation during the observed interval. The fixture exited successfully. Evidence is `/tmp/time26-gpu-probe-2a923765b3544335b1d28662a3cf7973` in Colima. It used a fixture-only attestation substitute and does not establish crash recovery, cancellation, real GPU operation, or production attestation.

### Promotion recovery review and staged build

On 2026-11-17, an isolated SQLite check confirmed that an exact v2 promotion retry remains pending, a different promotion is refused while the batch exists, and the batch prevents policy changes for that UID. A separate mocked-controller check covered cancellation receipt consumption, idempotent retry, and identity mismatch. These checks do not exercise the complete cancellation-to-clear path or crash recovery against Slurm. Static review found and fixed a cancellation-before-clear recovery omission and the two batch/policy interlocks. Inability to establish the controller state remains fail-closed.

The x86_64 candidate in `/tmp/time26-candidate-XUqx52TZ` was built on pro6000d in a private staging directory, with source reconstruction and artifact evidence recorded in `strict-priority-build-evidence.json`. This is not itself a deployment. Cross-file WAL/checkpoint/witness divergence is not automatically repaired and requires operator investigation; an older controller cannot consume the new RELEASE3 records. Do not restore an old ledger over a live one.
