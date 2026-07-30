"""Guard against drift between strings.json and translations/*.json."""

from __future__ import annotations

import json
import re
import warnings
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "cap_alerts"
_STRINGS = _PKG / "strings.json"
_TRANSLATIONS = _PKG / "translations"
_EN = _TRANSLATIONS / "en.json"
_CONFIG_FLOW = _PKG / "config_flow.py"

# Locales other than English, discovered rather than listed so a new
# translation is picked up by the parity checks the moment it lands.
_OTHER_LOCALES = sorted(p for p in _TRANSLATIONS.glob("*.json") if p != _EN)
_OTHER_IDS = [p.stem for p in _OTHER_LOCALES]


class TranslationDriftWarning(UserWarning):
    """A locale is missing keys that exist in strings.json.

    Reported rather than raised: Home Assistant falls back to English per
    key, so a lagging locale is cosmetic, and the author of a feature PR is
    usually not the person able to translate it. Promote to an error for a
    single run with ``-W error::UserWarning``.
    """


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


def test_locales_discovered() -> None:
    # Without this the parametrized checks below would silently collect
    # nothing if the translations directory were moved or renamed.
    assert _EN.is_file(), "translations/en.json is missing"
    assert _OTHER_LOCALES, "no non-English translations found"


@pytest.mark.parametrize("path", _OTHER_LOCALES, ids=_OTHER_IDS)
def test_translation_has_no_unknown_keys(path: Path) -> None:
    unknown = _keys(_load(path)) - _keys(_load(_STRINGS))
    assert not unknown, (
        f"{path.name} declares keys absent from strings.json "
        f"(stale or misspelled, and never rendered): {sorted(unknown)}"
    )


@pytest.mark.parametrize("path", _OTHER_LOCALES, ids=_OTHER_IDS)
def test_translation_completeness(path: Path) -> None:
    # Deliberately does not fail the suite — see TranslationDriftWarning.
    missing = _keys(_load(_STRINGS)) - _keys(_load(path))
    if missing:
        warnings.warn(
            f"{path.name} is missing {len(missing)} key(s) present in "
            f"strings.json: {sorted(missing)}",
            TranslationDriftWarning,
            stacklevel=2,
        )
