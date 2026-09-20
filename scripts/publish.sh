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

Creates a git tag and GitHub Release from main, with the HACS install asset
(hacs.json "filename", built from the tagged tree) attached.

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

# The release asset is what HACS installs (hacs.json: zip_release + filename),
# and it is the only download GitHub counts: a source archive never shows up
# in the release's download_count, which is why every integration release
# before v0.6.0 reads as zero installs. HACS extracts the zip straight into
# custom_components/cap_alerts/, so the integration's files sit at the zip
# root with no wrapping directory. Archiving the tagged tree rather than the
# working copy keeps __pycache__ and anything untracked out of it.
ASSET_NAME=$(python -c "import json; print(json.load(open('hacs.json'))['filename'])")
ASSET_DIR=$(mktemp -d)
ASSET="$ASSET_DIR/$ASSET_NAME"
git archive --format=zip -o "$ASSET" "$TAG:custom_components/cap_alerts"
echo "Built $ASSET_NAME from $TAG ($(python -c "import sys, zipfile; print(len(zipfile.ZipFile(sys.argv[1]).namelist()))" "$ASSET") entries)"

if gh release view "$TAG" >/dev/null 2>&1; then
  echo "Release $TAG already exists, skipping"
  if gh release view "$TAG" --json assets --jq '.assets[].name' | grep -qx "$ASSET_NAME"; then
    echo "Asset $ASSET_NAME already attached, skipping"
  else
    gh release upload "$TAG" "$ASSET"
    echo "Asset $ASSET_NAME attached to $TAG"
  fi
else
  gh release create "$TAG" \
    --title "$TAG" \
    --notes "$NOTES" \
    $([ "$PRERELEASE" = true ] && echo "--prerelease") \
    "$ASSET"
  echo "Release $TAG published with $ASSET_NAME"
fi

rm -rf "$ASSET_DIR"
