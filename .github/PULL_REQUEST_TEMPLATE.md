## Summary

<!-- What does this PR do and why? -->

## Changes

-

## Test Plan

<!-- Same invocations CI runs — see AGENTS.md "Build & Test Commands" -->

- [ ] `pytest tests -q` passes
- [ ] `ruff check custom_components/ tests/ scripts/` passes
- [ ] `ruff format --diff custom_components/ tests/ scripts/` is clean
- [ ] `mypy custom_components/cap_alerts` passes
- [ ] Loaded in a running Home Assistant instance (for behavior changes; `scripts/flow_walk.py` for config-flow changes)
- [ ] Dependency order respected (model → providers → coordinator → sensor → config_flow → __init__)

## Related Issues

<!-- Closes #... -->
