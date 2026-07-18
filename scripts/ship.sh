#!/bin/bash
# The ONLY way a stable tag gets made: gate, commit, tag, push - one
# script, set -e, no compound-statement seams. Usage:
#   scripts/ship.sh <tag> <commit-message-file>
set -euo pipefail
cd "$(dirname "$0")/.."
TAG="$1"; MSG="$2"
./tests/gate.sh
git add -A
git commit -F "$MSG"
git tag "$TAG"
git push -q origin main --tags
echo "SHIPPED $TAG (gate was green)"
