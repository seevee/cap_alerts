#!/usr/bin/env bash
# Run every gate CI runs, plus the one it can't, and print one line.
#
# The last line of output is the summary, anchored to the HEAD sha, and is
# meant to be pasted into a PR body verbatim rather than paraphrased:
#
#   verify @ 418d6e7: pytest 1199 passed; cov 97% (floor 96); flow cov 100%;
#   ruff check ok; ruff format ok; mypy ok; flow_walk skipped
#
# Every gate runs even after one fails, so the line is complete either way.
# Full output per gate lands in a log directory, printed on failure.
#
# flow_walk.py is off by default because it walks the *deployed* HA instance,
# not the working tree: pass --flow only after deploying the code under test
# (see the dev-environment memory note for the deploy/restart commands).
#
# Usage: scripts/verify.sh [--flow] [--skip-network]
#   --flow          also run scripts/flow_walk.py against the dev instance
#   --skip-network  passed through to flow_walk.py (omit MeteoAlarm/WMO fetches)

set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

FLOW=false
FLOW_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --flow) FLOW=true ;;
    --skip-network) FLOW_ARGS+=("--skip-network") ;;
    -h | --help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
  shift
done

if [[ ! -x .venv/bin/python ]]; then
  echo "no .venv; see AGENTS.md 'Build & Test Commands' for the one-time setup" >&2
  exit 2
fi

# Log directory: $VERIFY_LOGDIR if set, else under $TMPDIR.
LOGDIR="${VERIFY_LOGDIR:-${TMPDIR:-/tmp}}/cap_alerts-verify-$(date +%Y%m%dT%H%M%S)"
mkdir -p "$LOGDIR"

SHA="$(git rev-parse --short HEAD)"
DIRTY=""
if ! git diff --quiet || ! git diff --cached --quiet; then
  DIRTY=" (dirty)"
fi

FAILED=()
PARTS=()

# run <label> <logfile> <cmd...>
# Returns the command's exit status; output goes to the log only.
run() {
  local label="$1" log="$2"
  shift 2
  echo "==> $label"
  "$@" >"$LOGDIR/$log" 2>&1
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    FAILED+=("$label")
  fi
  return $rc
}

# --- pytest + overall coverage (same invocation CI runs) -----------------
run "pytest" pytest.log \
  .venv/bin/python -m pytest tests -q --cov --cov-fail-under=96
PYTEST_RC=$?
# Last "N passed[, M failed][, K skipped] in Xs" line, minus the timing.
PYTEST_LINE="$(grep -E '^(=+ )?[0-9]+ (passed|failed)' "$LOGDIR/pytest.log" \
  | tail -1 | sed -E 's/^=+ //; s/ in [0-9.]+s.*$//; s/ =+$//')"
COV_PCT="$(grep -E '^TOTAL' "$LOGDIR/pytest.log" | awk '{print $NF}' | tail -1)"
if [[ $PYTEST_RC -eq 0 ]]; then
  PARTS+=("pytest ${PYTEST_LINE:-ok}")
else
  PARTS+=("pytest FAILED (${PYTEST_LINE:-no summary})")
fi
PARTS+=("cov ${COV_PCT:-?} (floor 96)")

# --- config-flow coverage, gated at 100% (reads the .coverage file above) --
run "config-flow coverage" flowcov.log \
  .venv/bin/coverage report \
  --include='custom_components/cap_alerts/config_flow.py,custom_components/cap_alerts/flows/*' \
  --fail-under=100
FLOWCOV_RC=$?
FLOWCOV_PCT="$(grep -E '^TOTAL' "$LOGDIR/flowcov.log" | awk '{print $NF}' | tail -1)"
if [[ $FLOWCOV_RC -eq 0 ]]; then
  PARTS+=("flow cov ${FLOWCOV_PCT:-100%}")
else
  PARTS+=("flow cov FAILED (${FLOWCOV_PCT:-?})")
fi

# --- ruff check / format (CI checks custom_components/, tests/, scripts/) --
if run "ruff check" ruff-check.log \
  .venv/bin/ruff check custom_components/ tests/ scripts/; then
  PARTS+=("ruff check ok")
else
  PARTS+=("ruff check FAILED ($(grep -Eo 'Found [0-9]+ error[s]?' "$LOGDIR/ruff-check.log" | tail -1))")
fi

if run "ruff format" ruff-format.log \
  .venv/bin/ruff format --diff custom_components/ tests/ scripts/; then
  PARTS+=("ruff format ok")
else
  PARTS+=("ruff format FAILED ($(grep -Eo '[0-9]+ files? would be reformatted' "$LOGDIR/ruff-format.log" | tail -1))")
fi

# --- mypy (the integration only; scripts/ is standalone dev tooling) -------
if run "mypy" mypy.log .venv/bin/mypy custom_components/cap_alerts; then
  PARTS+=("mypy ok")
else
  PARTS+=("mypy FAILED ($(grep -Eo 'Found [0-9]+ error[s]?' "$LOGDIR/mypy.log" | tail -1))")
fi

# --- flow_walk against the deployed dev instance (opt-in) ------------------
if [[ $FLOW == true ]]; then
  if run "flow_walk" flow-walk.log scripts/flow_walk.py "${FLOW_ARGS[@]}"; then
    PARTS+=("flow_walk $(grep -E '^[0-9]+ checks,' "$LOGDIR/flow-walk.log" | tail -1)")
  else
    PARTS+=("flow_walk FAILED ($(grep -E '^[0-9]+ checks,' "$LOGDIR/flow-walk.log" | tail -1 || echo 'no summary'))")
  fi
else
  PARTS+=("flow_walk skipped")
fi

# --- summary ----------------------------------------------------------------
SUMMARY="verify @ ${SHA}${DIRTY}:"
SEP=" "
for part in "${PARTS[@]}"; do
  SUMMARY+="${SEP}${part}"
  SEP="; "
done
printf '%s\n' "$SUMMARY" >"$LOGDIR/summary.txt"

echo
if [[ ${#FAILED[@]} -gt 0 ]]; then
  echo "failed: ${FAILED[*]}"
  echo "logs: $LOGDIR"
fi
echo "$SUMMARY"

[[ ${#FAILED[@]} -eq 0 ]]
