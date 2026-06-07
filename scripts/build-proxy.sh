#!/usr/bin/env bash
# Cross-compile the BitBang Go proxy and bundle it into the plugin.
#
# The proxy binaries are NOT committed to this repo; they're built from the
# bitbang-cli Go source — by CI for releases (.github/workflows/publish.yml),
# and by this script for local editable-install testing.
#
# Usage:
#   scripts/build-proxy.sh [path-to-bitbang-cli]   (default: ../bitbang-cli)
#
# Pure-Go static builds (CGO_ENABLED=0) so they cross-compile from any host with
# no C toolchain. Output lands in octoprint_bitbang/bin/, where the plugin
# selects the right one at runtime via platform.machine().
set -euo pipefail

SRC="${1:-../bitbang-cli}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/octoprint_bitbang/bin"

if [ ! -d "$SRC/cmd/bitbang" ]; then
    echo "error: bitbang-cli source not found at '$SRC' (need $SRC/cmd/bitbang)" >&2
    echo "usage: scripts/build-proxy.sh [path-to-bitbang-cli]" >&2
    exit 1
fi
SRC="$(cd "$SRC" && pwd)"

mkdir -p "$OUT"

build() {
    local goarch="$1" goarm="$2" name="$3"
    echo "building $name (GOARCH=$goarch${goarm:+ GOARM=$goarm})..."
    ( cd "$SRC" && CGO_ENABLED=0 GOOS=linux GOARCH="$goarch" GOARM="$goarm" \
        go build -trimpath -ldflags '-s -w' -o "$OUT/$name" ./cmd/bitbang )
    chmod +x "$OUT/$name"
}

# Arches the plugin bundles (Raspberry Pi 3/4/5, 32- and 64-bit).
# Add amd64 / armv6 here to widen support.
build arm64 ""  bitbang-linux-arm64
build arm   "7" bitbang-linux-armv7

echo "bundled into $OUT:"
ls -la "$OUT"
