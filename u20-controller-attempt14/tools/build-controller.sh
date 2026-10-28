#!/bin/sh
set -eu

VERSION=19.05.5-u20-monthly.1
TAG=slurm-19-05-5-1
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
SOURCE_DIR=
OUTPUT_DIR=${PWD}/build-output
CONFIGURE_ARGS=

usage() {
    echo "usage: $0 --source-dir /path/to/slurm-19-05-5-1 [--output-dir NEW_DIR] [--configure-args '...']" >&2
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --source-dir) SOURCE_DIR=${2-}; shift 2 ;;
        --output-dir) OUTPUT_DIR=${2-}; shift 2 ;;
        --configure-args) CONFIGURE_ARGS=${2-}; shift 2 ;;
        *) usage ;;
    esac
done
[ -n "$SOURCE_DIR" ] || usage
[ -d "$SOURCE_DIR/.git" ] || { echo "source-dir must be a git checkout" >&2; exit 1; }
[ ! -e "$OUTPUT_DIR" ] || { echo "output-dir already exists; choose a new verified empty directory: $OUTPUT_DIR" >&2; exit 1; }

TAG_ACTUAL=$(git -C "$SOURCE_DIR" describe --tags --exact-match 2>/dev/null || true)
[ "$TAG_ACTUAL" = "$TAG" ] || { echo "expected exact tag $TAG, got ${TAG_ACTUAL:-unknown}" >&2; exit 1; }
test -z "$(git -C "$SOURCE_DIR" status --porcelain)" || { echo "source checkout has local changes or untracked files" >&2; exit 1; }
mkdir -p "$OUTPUT_DIR/source"
cp -a "$SOURCE_DIR/." "$OUTPUT_DIR/source/"
git -C "$OUTPUT_DIR/source" apply "$ROOT/patches/0001-strict-monthly-quota-controller-19.05.5.patch"

if [ -n "$CONFIGURE_ARGS" ]; then
    sh -c "cd \"$OUTPUT_DIR/source\" && ./configure $CONFIGURE_ARGS"
else
    (cd "$OUTPUT_DIR/source" && ./configure)
fi
(cd "$OUTPUT_DIR/source" && make -C src/slurmctld -j"${MAKE_JOBS:-2}" slurmctld)

mkdir -p "$OUTPUT_DIR/artifact/sbin" "$OUTPUT_DIR/artifact/source-patch"
BINARY="$OUTPUT_DIR/source/src/slurmctld/.libs/slurmctld"
[ -x "$BINARY" ] || { echo "compiled ELF not found: $BINARY" >&2; exit 1; }
cp "$BINARY" "$OUTPUT_DIR/artifact/sbin/slurmctld"
cp "$ROOT/patches/0001-strict-monthly-quota-controller-19.05.5.patch" "$OUTPUT_DIR/artifact/source-patch/"
sha256sum "$OUTPUT_DIR/artifact/sbin/slurmctld" > "$OUTPUT_DIR/artifact/SHA256SUMS"
cat > "$OUTPUT_DIR/artifact/BUILD-MANIFEST" <<EOF
version=$VERSION
upstream_tag=$TAG
source_commit=$(git -C "$SOURCE_DIR" rev-parse HEAD)
monthly_controller_state_default=/var/lib/slurm/monthly-quota
enforcement_activation=blocked_until_linearization_bounded_io_and_tres_review
adapter_scope=slurm-19.05.5-u20-ordinary-local-gpu
EOF
echo "Built versioned artifact at $OUTPUT_DIR/artifact"
