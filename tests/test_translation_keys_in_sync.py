"""Guard against drift between strings.json and translations/en.json."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "cap_alerts"
_STRINGS = _PKG / "strings.json"
_EN = _PKG / "translations" / "en.json"
_CONFIG_FLOW = _PKG / "config_flow.py"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _keys(node: object, prefix: str = "") -> set[str]:
    if not isinstance(node, dict):
        return set()
    out: set[str] = set()
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        out.add(path)
        out |= _keys(value, path)
    return out


@pytest.mark.parametrize("section", ["step", "error", "abort"])
def test_config_keys_match(section: str) -> None:
    strings = _load(_STRINGS)["config"].get(section, {})
    en = _load(_EN)["config"].get(section, {})
    assert _keys(strings) == _keys(en), (
        f"strings.json and translations/en.json disagree on config.{section}"
    )


def test_step_ids_in_config_flow_have_strings() -> None:
    text = _CONFIG_FLOW.read_text(encoding="utf-8")
    referenced = set(re.findall(r'step_id="([^"]+)"', text))
    for path in (_STRINGS, _EN):
        data = _load(path)
        known = set(data["config"]["step"].keys()) | set(
            data.get("options", {}).get("step", {}).keys()
        )
        missing = referenced - known
        assert not missing, f"step_ids missing from {path.name}: {missing}"
