#!/bin/bash
# Bump the version number in all files that contain it.
#
# Usage:
#   ./version.sh 0.2.0

set -e

if [ -z "$1" ]; then
    echo "Usage: $0 <version>"
    echo "Current version: $(grep '^version' pyproject.toml | head -1 | sed 's/.*"\(.*\)"/\1/')"
    exit 1
fi

NEW="$1"
DIR="$(cd "$(dirname "$0")" && pwd)"

# Update pyproject.toml
sed -i "s/^version = \".*\"/version = \"$NEW\"/" "$DIR/pyproject.toml"

# Update octoprint_bitbang/__init__.py (OctoPrint reads __plugin_version__)
sed -i "s/^__plugin_version__ = \".*\"/__plugin_version__ = \"$NEW\"/" "$DIR/octoprint_bitbang/__init__.py"

echo "Version bumped to $NEW"
grep '^version' "$DIR/pyproject.toml"
grep '^__plugin_version__' "$DIR/octoprint_bitbang/__init__.py"
