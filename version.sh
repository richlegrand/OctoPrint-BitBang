#!/bin/bash
# Bump the plugin version.
#
# The version lives only in pyproject.toml; the plugin reads
# __plugin_version__ from the installed package metadata at runtime, so there's
# nothing to update in octoprint_bitbang/__init__.py.
#
# Usage:
#   ./version.sh 0.2.7

set -e

if [ -z "$1" ]; then
    echo "Usage: $0 <version>"
    echo "Current version: $(grep '^version' pyproject.toml | head -1 | sed 's/.*"\(.*\)"/\1/')"
    exit 1
fi

NEW="$1"
DIR="$(cd "$(dirname "$0")" && pwd)"

sed -i "s/^version = \".*\"/version = \"$NEW\"/" "$DIR/pyproject.toml"

echo "Version bumped to $NEW"
grep '^version' "$DIR/pyproject.toml"
