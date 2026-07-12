#!/usr/bin/env bash
set -euo pipefail

# -------------------------
# Activate venv if available
# -------------------------

if [ -d ".venv" ]; then
  . .venv/bin/activate
fi

usage() {
  cat <<'USAGE'
Usage: publish.sh <version>

Creates a git tag and GitHub Release from main.

Examples:
  ./scripts/publish.sh 0.3.0
  ./scripts/publish.sh 0.3.0-alpha.1
USAGE
  exit 1
}

[[ $# -lt 1 ]] && usage

VERSION=$1
TAG="v$VERSION"

PRERELEASE=false
if [[ "$VERSION" == *"-alpha"* || "$VERSION" == *"-beta"* || "$VERSION" == *"-rc"* ]]; then
  PRERELEASE=true
fi

echo "Publishing v$VERSION"

git checkout main
git pull origin main

if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "Tag $TAG already exists locally, skipping"
else
  git tag "$TAG"
fi

if git ls-remote --tags origin "$TAG" | grep -q "$TAG"; then
  echo "Tag $TAG already exists on remote, skipping push"
else
  git push origin "$TAG"
fi

# Generate release notes from git-cliff
CLIFF_FLAGS=()
if [ "$PRERELEASE" = true ]; then
  NOTES=$(pip install "$(grep '^git-cliff' requirements_test.txt)" 2>/dev/null \
    && git-cliff --config cliff.toml --latest --strip header)
else
  NOTES=$(pip install "$(grep '^git-cliff' requirements_test.txt)" 2>/dev/null \
    && git-cliff --config cliff.toml --tag-pattern "^v[0-9]+\.[0-9]+\.[0-9]+$" --latest --strip header)
fi

if gh release view "$TAG" >/dev/null 2>&1; then
  echo "Release $TAG already exists, skipping"
else
  gh release create "$TAG" \
    --title "$TAG" \
    --notes "$NOTES" \
    $([ "$PRERELEASE" = true ] && echo "--prerelease")
  echo "Release $TAG published"
fi
