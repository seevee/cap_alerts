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

# Generate release notes from git-cliff. Install it outside the command
# substitution: pip writes "Requirement already satisfied" to stdout, which
# otherwise lands at the top of the release body.
pip install "$(grep '^git-cliff' requirements_test.txt)" >/dev/null 2>&1 || true

# First-time contributors come from GitHub (cliff.toml [remote.github]); gh's
# token lifts the rate limit.
export GITHUB_TOKEN="${GITHUB_TOKEN:-$(gh auth token)}"

CLIFF_FLAGS=()
if [ "$PRERELEASE" = false ]; then
  CLIFF_FLAGS+=(--tag-pattern "^v[0-9]+\.[0-9]+\.[0-9]+$")
fi

NOTES=$(CLIFF_SURFACE=release git-cliff --config cliff.toml "${CLIFF_FLAGS[@]}" --latest --strip header)

# The release PR body is the release notes once someone has edited it.
# release.sh seeds that body with the same generated notes, so a body that
# still matches the generation was never touched and the fresh copy ships.
# Anything else, a narrative above the list or an annotated line, ships
# verbatim: the PR is where the notes get reviewed, not the release edit box.
PR_BODY=$(gh pr list --state merged --base main --head "release/v$VERSION" \
  --json body --jq '.[0].body // empty' 2>/dev/null || true)
PR_BODY=${PR_BODY//$'\r'/}
if [ -n "$PR_BODY" ] && [ "$PR_BODY" != "$NOTES" ]; then
  echo "Using the release PR body as the release notes"
  NOTES="$PR_BODY"
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
