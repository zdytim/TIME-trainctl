#!/usr/bin/env python3
"""Assemble the u20 broker candidate from committed inputs without deploying it."""
import argparse
import ast
import hashlib
import pathlib
import subprocess
import sys

BASE_HASH = "65d213100a0e0ebd3f9fe87b51d1625f8539acf954ca572b43fa48a07c43173c"
PATCHES = (
    "u20-runtime-baseline-from-tracked.patch",
    "u20-backend-idempotency.patch",
    "u20-broker-controller-read.patch",
    "u20-broker-monthly-grant.patch",
    "u20-broker-monthly-capabilities.patch",
    "u20-broker-month-retry-fix.patch",
    "u20-broker-grant-month-notice-fix.patch",
    "u20-broker-console-monthly-view.patch",
    "u20-broker-probe-privileges.patch",
)


def checked(repo, relative):
    path = repo / relative
    if not path.is_file() or path.is_symlink():
        raise ValueError("input missing or symlink: " + relative)
    committed = subprocess.run(["git", "show", "HEAD:" + relative], cwd=str(repo),
                               check=True, stdout=subprocess.PIPE).stdout
    if committed != path.read_bytes():
        raise ValueError("input differs from committed HEAD: " + relative)
    return committed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=pathlib.Path,
                        help="new isolated directory; must not already exist")
    args = parser.parse_args()
    repo = pathlib.Path(__file__).resolve().parents[2]
    base = checked(repo, "u20-training-platform/backend/pro6000d-training-api.py")
    bridge = checked(repo, "u20-training-platform/backend/controller_bridge.py")
    view = checked(repo, "u20-training-platform/backend/controller_view.py")
    patches = [(name, checked(repo, "research/slurm19/" + name)) for name in PATCHES]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo), text=True).strip()
    output = args.output.absolute()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    broker = output / "pro6000d-training-api.py"
    broker.write_bytes(base)
    (output / "controller_bridge.py").write_bytes(bridge)
    (output / "controller_view.py").write_bytes(view)
    for index, (name, data) in enumerate(patches):
        patch = output / ("%02d-" % index + name)
        patch.write_bytes(data)
        subprocess.run(["patch", "--batch", "--forward", "-p1", "-i", str(patch)], cwd=str(output), check=True)
        if index == 0 and hashlib.sha256(broker.read_bytes()).hexdigest() != BASE_HASH:
            raise ValueError("reconstructed u20 runtime baseline hash mismatch")
    for path in (broker, output / "controller_bridge.py", output / "controller_view.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    print("project commit:", revision)
    print("u20 baseline SHA-256:", BASE_HASH)
    print("broker candidate SHA-256:", hashlib.sha256(broker.read_bytes()).hexdigest())
    print("Candidate only; no database, service, or system installation was changed.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError) as exc:
        print("Broker assembly failed; preserve the output for review: %s" % exc, file=sys.stderr)
        sys.exit(1)
