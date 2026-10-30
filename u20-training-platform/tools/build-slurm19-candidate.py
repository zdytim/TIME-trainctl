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
    "grant-scheduler-wakeup.patch", "tier-stale-queue-fix.patch",
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
    args = parser.parse_args()
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
        configure = ["./configure", "--prefix=" + str(output / "stage"),
                     "--sysconfdir=" + str(output / "etc"),
                     "--localstatedir=" + str(output / "var")]
        with (output / "configure.log").open("xb") as log:
            subprocess.run(configure, cwd=str(source), stdout=log, stderr=subprocess.STDOUT, check=True)
        with (output / "build.log").open("xb") as log:
            subprocess.run(["nice", "-n", "19", "make", "-j2", "-C", "src"], cwd=str(source),
                           stdout=log, stderr=subprocess.STDOUT, check=True)
        binary = source / "src/slurmctld/.libs/slurmctld"
        print("isolated controller SHA-256:", digest(binary), flush=True)
    print("Candidate only; no system installation or quota activation was performed.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print("Candidate build failed; inspect the retained output directory: %s" % exc, file=sys.stderr)
        sys.exit(1)
