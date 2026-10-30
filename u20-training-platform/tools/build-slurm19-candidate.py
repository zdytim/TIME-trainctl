#!/usr/bin/env python3
"""Reconstruct and optionally build the isolated u20 Slurm 19 candidate.

This never installs system files or runs tests. The supplied archive must be the
pinned official source, and the destination must not already exist.
"""
import argparse
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile

ARCHIVE_SHA256 = "1ce10e864bcbd3770f13de5b5eea4821420fcbce39fd6211426b068de98c3b67"
SOURCE_COMMIT = "78e9cfab783aeed0da68ea25532a3f35114d4072"
PATCHES = (
    "staged-controller.patch", "ledger-recovery-fix.patch", "grant-idempotency-fix.patch",
    "reconcile-prepared-fix.patch", "tier-projection.patch", "tier-snapshot.patch",
    "recovery-ready-fix.patch", "controller-build-node-mgr-fix.patch",
    "tier-scheduler-integration.patch", "ledger-checkpoint.patch", "checkpoint-parser-fix.patch",
    "grant-global-id-fix.patch", "controller-read-snapshot.patch", "cpu-array-shape-fix.patch",
    "controller-local-bridge.patch", "controller-local-bridge-io-fix.patch",
    "selector-ambiguous-fix.patch", "replay-identity-fix.patch", "unhealthy-release-fix.patch",
    "grant-scheduler-wakeup.patch", "tier-stale-queue-fix.patch", "quota-denial-timestamps-fix.patch",
    "untracked-completing-recovery-fix.patch", "uncommitted-release-fix.patch", "strict-grant-replay-fix.patch",
    "wal-empty-fields-fix.patch", "typed-gpu-admission-fix.patch", "single-node-gres-fallback.patch",
    "gres-device-bitmap-fix.patch", "backfill-exhausted-skip.patch", "wal-bounded-line-fix.patch",
    "public-gres-api-fix.patch", "calendar-year-fix.patch", "selector-count-only-fix.patch",
    "cpu-only-queue-failure-fix.patch",
    "stale-control-socket-fix.patch",
)

# u20's live configuration requires these plugins.  A successful compiler
# exit alone does not mean configure found the development dependencies.
U20_REQUIRED_PLUGINS = (
    "src/plugins/auth/munge/.libs/auth_munge.so",
    "src/plugins/cred/munge/.libs/cred_munge.so",
    "src/plugins/job_submit/lua/.libs/job_submit_lua.so",
    "src/plugins/sched/backfill/.libs/sched_backfill.so",
    "src/plugins/select/cons_tres/.libs/select_cons_tres.so",
    "src/plugins/gres/gpu/.libs/gres_gpu.so",
    "src/plugins/task/cgroup/.libs/task_cgroup.so",
    "src/plugins/proctrack/cgroup/.libs/proctrack_cgroup.so",
    "src/plugins/jobacct_gather/linux/.libs/jobacct_gather_linux.so",
    "src/plugins/accounting_storage/none/.libs/accounting_storage_none.so",
    "src/plugins/acct_gather_energy/none/.libs/acct_gather_energy_none.so",
    "src/plugins/ext_sensors/none/.libs/ext_sensors_none.so",
    "src/plugins/mpi/none/.libs/mpi_none.so",
    "src/plugins/preempt/none/.libs/preempt_none.so",
    "src/plugins/priority/basic/.libs/priority_basic.so",
    "src/plugins/switch/none/.libs/switch_none.so",
    "src/plugins/topology/none/.libs/topology_none.so",
    "src/plugins/jobcomp/filetxt/.libs/jobcomp_filetxt.so",
    "src/plugins/job_container/none/.libs/job_container_none.so",
    "src/plugins/launch/slurm/.libs/launch_slurm.so",
)


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def checked_patch(repo, name):
    relative = "research/slurm19/" + name
    path = repo / relative
    if not path.is_file() or path.is_symlink():
        raise ValueError("patch is missing or is a symlink: " + relative)
    committed = subprocess.run(["git", "show", "HEAD:" + relative], cwd=str(repo),
                               check=True, stdout=subprocess.PIPE).stdout
    if committed != path.read_bytes():
        raise ValueError("patch differs from committed HEAD: " + relative)
    return path


def extract_verified(archive, root):
    root.mkdir(mode=0o700)
    with tarfile.open(str(archive), "r:gz") as tar:
        for member in tar:
            path = pathlib.PurePosixPath(member.name)
            if path.is_absolute() or not path.parts or any(p in ("", ".", "..") for p in path.parts):
                raise ValueError("unsafe archive member: " + member.name)
            if not member.isdir() and not member.isfile():
                raise ValueError("links and special archive members are refused: " + member.name)
            target = root.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
                os.chmod(str(target), member.mode & 0o777)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--build", action="store_true", help="configure and compile src in the isolated directory")
    parser.add_argument("--layout", choices=("isolated", "u20-release-candidate"), default="isolated",
                        help="u20 layout embeds prospective system paths; never installs or starts it")
    parser.add_argument("--dependency-root", type=pathlib.Path,
                        help="optional build-only root containing munge and lua51 include/lib directories")
    args = parser.parse_args()
    if args.layout != "isolated" and not args.build:
        raise ValueError("a non-isolated layout requires --build")
    repo = pathlib.Path(__file__).resolve().parents[2]
    archive = args.archive.resolve(strict=True)
    output = args.output.absolute()
    if digest(archive) != ARCHIVE_SHA256:
        raise ValueError("source archive SHA-256 mismatch")
    patches = [checked_patch(repo, name) for name in PATCHES]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo), text=True).strip()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    source = output / "source"
    print("source commit:", SOURCE_COMMIT, "project commit:", revision, flush=True)
    extract_verified(archive, source)
    for patch in patches:
        print("applying", patch.name, flush=True)
        subprocess.run(["patch", "--batch", "--forward", "-p1", "-i", str(patch)],
                       cwd=str(source), check=True)
    if args.build:
        if args.layout == "u20-release-candidate":
            # These are embedded path strings only. Do not create or install
            # into them, and do not run this candidate against a live system.
            prefix = "/opt/time/slurm19-" + revision[:12]
            sysconfdir, localstatedir = "/etc/slurm-llnl", "/var"
            print("WARNING: candidate embeds live u20 config path; do not execute it", flush=True)
        else:
            prefix = str(output / "stage")
            sysconfdir, localstatedir = str(output / "etc"), str(output / "var")
        print("build layout:", args.layout, "prefix:", prefix,
              "sysconfdir:", sysconfdir, "localstatedir:", localstatedir, flush=True)
        configure = ["./configure", "--prefix=" + prefix,
                     "--sysconfdir=" + sysconfdir,
                     "--localstatedir=" + localstatedir]
        env = os.environ.copy()
        if args.dependency_root:
            deps = args.dependency_root.resolve(strict=True)
            if any(c.isspace() for c in str(deps)):
                raise ValueError("dependency root cannot contain whitespace")
            required = ("munge/include/munge.h", "munge/lib/libmunge.so",
                        "lua51/include/lua.h", "lua51/lib/liblua5.1.so")
            if any(not (deps / path).is_file() for path in required):
                raise ValueError("dependency root is missing required headers or link libraries")
            configure.extend(["--with-munge=" + str(deps / "munge"), "--without-rpath"])
            env["lua_CFLAGS"] = "-I" + str(deps / "lua51/include")
            env["lua_LIBS"] = "-L" + str(deps / "lua51/lib") + " -llua5.1 -lm"
            print("build-only dependency root:", deps, flush=True)
        with (output / "configure.log").open("xb") as log:
            subprocess.run(configure, cwd=str(source), env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        with (output / "build.log").open("xb") as log:
            subprocess.run(["nice", "-n", "19", "make", "-j2", "-C", "src"], cwd=str(source),
                           stdout=log, stderr=subprocess.STDOUT, check=True)
        if args.layout == "u20-release-candidate":
            missing = [name for name in U20_REQUIRED_PLUGINS if not (source / name).is_file()]
            if missing:
                raise ValueError("u20 release build is incomplete; required plugins missing: " + ", ".join(missing))
        binary = source / "src/slurmctld/.libs/slurmctld"
        print("candidate controller SHA-256:", digest(binary), flush=True)
    print("Candidate only; no system installation or quota activation was performed.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print("Candidate build failed; inspect the retained output directory: %s" % exc, file=sys.stderr)
        sys.exit(1)
