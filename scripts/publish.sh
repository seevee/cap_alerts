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
Usage: publish.sh <version> [--zone-data <file>]

Creates a git tag and GitHub Release from main, with the HACS install asset
(hacs.json "filename", built from the tagged tree) attached.

--zone-data attaches a built NWS zone dataset (the .gz from
build_zone_dataset.py --gzip) as a second asset, beside the zip and not inside
it. Pass it on the release that first carries the file's SHA-256 in
KNOWN_ZONE_DATA; the file is refused when the tagged code does not list it.

Examples:
  ./scripts/publish.sh 0.3.0
  ./scripts/publish.sh 0.3.0-alpha.1
  ./scripts/publish.sh 0.9.0-alpha.4 --zone-data plans/zone-dataset/out/cap_alerts.nws_zones.jsonl.gz
USAGE
  exit 1
}

[[ $# -lt 1 ]] && usage

VERSION=$1
TAG="v$VERSION"
shift

ZONE_DATA=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --zone-data)
      [[ $# -lt 2 ]] && usage
      ZONE_DATA=$(realpath "$2")
      shift 2
      ;;
    *) usage ;;
  esac
done

if [ -n "$ZONE_DATA" ] && [ ! -f "$ZONE_DATA" ]; then
  echo "Zone dataset not found: $ZONE_DATA" >&2
  exit 1
fi

PRERELEASE=false
if [[ "$VERSION" == *"-alpha"* || "$VERSION" == *"-beta"* || "$VERSION" == *"-rc"* ]]; then
  PRERELEASE=true
fi

echo "Publishing v$VERSION"

git checkout main
git pull origin main

# The dataset goes out only with code that will load it. The reader accepts a
# file by its SHA-256 alone (KNOWN_ZONE_DATA), so a file the tagged tree does
# not list would be a download every install refuses. Checked before the tag
# exists, so a wrong file leaves nothing to undo.
ZONE_MODULE=custom_components/cap_alerts/providers/nws_zone_data.py
# ZONE_DATA_FILENAME in that module: the name on disk and on the release.
ZONE_ASSET_NAME=cap_alerts.nws_zones.jsonl.gz
if [ -n "$ZONE_DATA" ]; then
  ZONE_REF=$(git rev-parse -q --verify "$TAG^{commit}" || git rev-parse HEAD)
  ZONE_SHA=$(sha256sum "$ZONE_DATA" | cut -d' ' -f1)
  if ! git show "$ZONE_REF:$ZONE_MODULE" | grep -q "\"$ZONE_SHA\""; then
    echo "Zone dataset SHA-256 $ZONE_SHA is not in KNOWN_ZONE_DATA at $ZONE_REF" >&2
    exit 1
  fi
  echo "Zone dataset $ZONE_SHA is listed at $ZONE_REF, publishing as $ZONE_ASSET_NAME"
fi

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

# The zone dataset rides the same release under its fixed name, whatever the
# built file was called: the download URL the integration pins is tag + name.
ASSETS=("$ASSET")
if [ -n "$ZONE_DATA" ]; then
  cp "$ZONE_DATA" "$ASSET_DIR/$ZONE_ASSET_NAME"
  ASSETS+=("$ASSET_DIR/$ZONE_ASSET_NAME")
fi

if gh release view "$TAG" >/dev/null 2>&1; then
  echo "Release $TAG already exists, skipping"
  ATTACHED=$(gh release view "$TAG" --json assets --jq '.assets[].name')
  for FILE in "${ASSETS[@]}"; do
    NAME=$(basename "$FILE")
    if grep -qx "$NAME" <<<"$ATTACHED"; then
      echo "Asset $NAME already attached, skipping"
    else
      gh release upload "$TAG" "$FILE"
      echo "Asset $NAME attached to $TAG"
    fi
  done
else
  gh release create "$TAG" \
    --title "$TAG" \
    --notes "$NOTES" \
    $([ "$PRERELEASE" = true ] && echo "--prerelease") \
    "${ASSETS[@]}"
  echo "Release $TAG published with $(basename -a "${ASSETS[@]}" | paste -sd' ')"
fi

rm -rf "$ASSET_DIR"
