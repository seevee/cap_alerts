#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=false
PRERELEASE=""
BUMP=""

# -------------------------
# Parse args
# -------------------------

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --alpha | --beta | --rc)
      PRERELEASE="${1#--}"
      shift
      ;;
    major | minor | patch)
      BUMP="$1"
      shift
      ;;
    *)
      echo "Unknown argument: $1"
      exit 1
      ;;
  esac
done

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)

echo "Release pipeline starting"
echo "Current branch: $CURRENT_BRANCH"
echo "Dry run: $DRY_RUN"
echo "Prerelease: ${PRERELEASE:-none}"

git fetch origin
git fetch --tags origin

# -------------------------
# Strict checks ONLY for real releases
# -------------------------

RESUMING=false

if [ "$DRY_RUN" = false ]; then
  BASE_REF="HEAD"

  if [[ "$CURRENT_BRANCH" =~ ^release/v(.+)$ ]]; then
    RESUMING=true
    RESUME_VERSION="${BASH_REMATCH[1]}"
    echo "Resuming release $RESUME_VERSION from existing branch"
  elif [ "$CURRENT_BRANCH" != "main" ]; then
    echo "Releases must be run from main (or a release/* branch for re-runs)"
    exit 1
  fi

  git fetch origin

  if [ "$RESUMING" = false ]; then
    if ! git diff --quiet; then
      echo "Working tree not clean"
      exit 1
    fi

    if ! git diff --quiet main origin/main; then
      echo "Local main not synced with origin/main"
      exit 1
    fi

    if python -m pytest tests -q --tb=no 2>/dev/null; then
      echo "Tests passed"
    else
      echo "Tests failed (run: pytest tests -q)"
      exit 1
    fi

    if ruff check custom_components/ tests/ 2>/dev/null; then
      echo "Lint passed"
    else
      echo "Lint failed (run: ruff check custom_components/ tests/)"
      exit 1
    fi
  fi

else
  BASE_REF="origin/main"
fi

# -------------------------
# Determine version
# -------------------------

if [ "$RESUMING" = true ]; then
  VERSION="$RESUME_VERSION"
  echo "Resuming version: $VERSION"
else
  if [ -z "$BUMP" ]; then
    LATEST_TAG=$(git describe --tags --abbrev=0 "$BASE_REF" 2>/dev/null || echo "")
    if [ -n "$LATEST_TAG" ]; then
      BUMP=$(git log "$LATEST_TAG".."$BASE_REF" --pretty=%s \
        | python -c "
import sys
major = minor = patch = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    if line.startswith(('feat', 'perf')):
        minor += 1
    elif line.startswith('fix'):
        patch += 1
if minor > 0:
    print('minor')
elif patch > 0:
    print('patch')
else:
    print('patch')
")
    fi
  fi

  # Get current version from manifest.json
  CURRENT=$(python -c "import json; print(json.load(open('custom_components/cap_alerts/manifest.json'))['version'])")

  # Extract base version from CURRENT (strip any pre-release suffix)
  BASE_VERSION=$(echo "$CURRENT" | sed -E 's/-(alpha|beta|rc)\.[0-9]+$//')

  LATEST_TAG=$(git describe --tags --abbrev=0 "$BASE_REF" 2>/dev/null || echo "")

  PROMOTE=false
  BASE_FROM_PRERELEASE=""

  if [[ "$CURRENT" =~ ^([0-9]+\.[0-9]+\.[0-9]+)-(alpha|beta|rc)\.[0-9]+$ ]]; then
    BASE_FROM_PRERELEASE="${BASH_REMATCH[1]}"
    COMMITS_AFTER=$(git rev-list "$LATEST_TAG"..$BASE_REF --count)
    if [ "$COMMITS_AFTER" -eq 0 ]; then
      PROMOTE=true
    fi
  fi

  # Compute version
  if [ "$PROMOTE" = true ]; then
    VERSION="$BASE_FROM_PRERELEASE"
    echo "Promoting prerelease $CURRENT → v$VERSION"
  elif [ -n "$PRERELEASE" ]; then
    # Determine base for bump
    if [[ "$CURRENT" =~ ^[0-9]+\.[0-9]+\.[0-9]+ ]]; then
      BASE="$CURRENT"
    else
      # Parse semver bump from base version
      IFS='.' read -r MAJOR MINOR PATCH <<< "$BASE_VERSION"
      case "$BUMP" in
        major) MAJOR=$((MAJOR + 1)); MINOR=0; PATCH=0 ;;
        minor) MINOR=$((MINOR + 1)); PATCH=0 ;;
        patch) PATCH=$((PATCH + 1)) ;;
      esac
      BASE="$MAJOR.$MINOR.$PATCH"
    fi

    EXISTING=$(git tag -l "v$BASE-$PRERELEASE.*" | wc -l | tr -d ' ')
    NEXT=$((EXISTING + 1))
    VERSION="$BASE-$PRERELEASE.$NEXT"
  else
    IFS='.' read -r MAJOR MINOR PATCH <<< "$BASE_VERSION"
    case "$BUMP" in
      major) MAJOR=$((MAJOR + 1)); MINOR=0; PATCH=0 ;;
      minor) MINOR=$((MINOR + 1)); PATCH=0 ;;
      patch) PATCH=$((PATCH + 1)) ;;
    esac
    VERSION="$MAJOR.$MINOR.$PATCH"
  fi

  echo "Current version: $CURRENT"
  echo "Next version: $VERSION"
fi

# -------------------------
# Determine cliff flags for note generation
# -------------------------

CLIFF_FLAGS=()

if [[ ! "$VERSION" =~ -(alpha|beta|rc)\. ]]; then
  CLIFF_FLAGS+=(--tag-pattern "^v[0-9]+\.[0-9]+\.[0-9]+$")
fi

# -------------------------
# Dry-run preview
# -------------------------

if [ "$DRY_RUN" = true ]; then
  echo ""
  echo "---- SIMULATED RELEASE FROM origin/main ----"
  pip install "$(grep '^git-cliff' requirements_test.txt)" 2>/dev/null
  python -m git_cliff \
    --config cliff.toml \
    --tag "v$VERSION" \
    "${CLIFF_FLAGS[@]}" \
    --unreleased \
    --strip header \
    "$BASE_REF"
  echo ""
  echo "Next tag: v$VERSION"
  echo "Branch would be: release/v$VERSION"
  echo "Dry run complete"
  exit 0
fi

BRANCH="release/v$VERSION"

# -------------------------
# Create or switch to branch
# -------------------------

if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  echo "Branch $BRANCH already exists, switching to it"
  git checkout "$BRANCH"
else
  git checkout -b "$BRANCH"
fi

# -------------------------
# Update manifest version
# -------------------------

CURRENT_MANIFEST=$(python -c "import json; print(json.load(open('custom_components/cap_alerts/manifest.json'))['version'])")

if [ "$CURRENT_MANIFEST" != "$VERSION" ]; then
  python -c "
import json
p = 'custom_components/cap_alerts/manifest.json'
d = json.load(open(p))
d['version'] = '$VERSION'
open(p, 'w').write(json.dumps(d, indent=2) + '\n')
"
else
  echo "Version already at $VERSION, skipping bump"
fi

# -------------------------
# Generate changelog
# -------------------------

pip install "$(grep '^git-cliff' requirements_test.txt)" 2>/dev/null
python -m git_cliff --config cliff.toml --tag "v$VERSION" "${CLIFF_FLAGS[@]}" --output CHANGELOG.md

git add CHANGELOG.md custom_components/cap_alerts/manifest.json

if git diff --cached --quiet; then
  echo "Nothing to commit, skipping"
else
  git commit -m "chore(release): set manifest version to $VERSION"
fi

# -------------------------
# Push branch
# -------------------------

if git rev-parse --verify --quiet "refs/remotes/origin/$BRANCH" >/dev/null 2>&1 \
   && git diff --quiet "$BRANCH" "origin/$BRANCH"; then
  echo "Branch already pushed and up to date, skipping push"
else
  git push -u origin "$BRANCH"
fi

# -------------------------
# Create PR
# -------------------------

NOTES=$(python -m git_cliff \
  --config cliff.toml \
  --tag "v$VERSION" \
  "${CLIFF_FLAGS[@]}" \
  --unreleased \
  --strip header \
  "$BASE_REF")

EXISTING_PR=$(gh pr list --head "$BRANCH" --base main --json number --jq '.[0].number // empty' 2>/dev/null || true)

if [ -n "$EXISTING_PR" ]; then
  echo "PR #$EXISTING_PR already exists, updating body"
  gh pr edit "$EXISTING_PR" --body "$NOTES"
else
  gh pr create \
    --title "chore(release): set manifest version to $VERSION" \
    --body "$NOTES" \
    --base main \
    --head "$BRANCH"
fi

echo ""
echo "PR created for v$VERSION"
echo ""
echo "After merge run:"
echo "scripts/publish.sh $VERSION"
