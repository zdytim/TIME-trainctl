#!/bin/sh
set -eu

TAG=slurm-19-05-5-1
REMOTE=https://github.com/SchedMD/slurm.git
DEST=${1-}
[ -n "$DEST" ] || { echo "usage: $0 /new/path/slurm-19-05-5-1" >&2; exit 2; }
test ! -e "$DEST" || { echo "destination already exists: $DEST" >&2; exit 1; }
git clone --branch "$TAG" --depth 1 "$REMOTE" "$DEST"
test "$(git -C "$DEST" describe --tags --exact-match)" = "$TAG"
git -C "$DEST" diff --quiet
echo "Fetched official SchedMD source tag $TAG at $DEST"
