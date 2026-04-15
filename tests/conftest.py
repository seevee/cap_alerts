"""Shared test fixtures."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _REPO_ROOT / "custom_components" / "cap_alerts"


def _load_submodule(name: str) -> types.ModuleType:
    """Load ``cap_alerts.<name>`` directly, bypassing the HA-dependent package init."""
    full = f"cap_alerts.{name}"
    if full in sys.modules:
        return sys.modules[full]
    # Ensure a stub parent package exists so relative imports resolve.
    if "cap_alerts" not in sys.modules:
        parent = types.ModuleType("cap_alerts")
        parent.__path__ = [str(_PKG_DIR)]
        sys.modules["cap_alerts"] = parent
    spec = importlib.util.spec_from_file_location(full, _PKG_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


# Pre-load submodules so tests can import ``custom_components.cap_alerts.*``.
_model = _load_submodule("model")
_icons = _load_submodule("icons")
_normalize = _load_submodule("normalize")
sys.modules["custom_components"] = types.ModuleType("custom_components")
sys.modules["custom_components"].__path__ = [str(_REPO_ROOT / "custom_components")]
sys.modules["custom_components.cap_alerts"] = sys.modules["cap_alerts"]
sys.modules["custom_components.cap_alerts.model"] = _model
sys.modules["custom_components.cap_alerts.icons"] = _icons
sys.modules["custom_components.cap_alerts.normalize"] = _normalize

CAPAlert = _model.CAPAlert


def make_alert(**overrides: Any) -> CAPAlert:
    """Build a CAPAlert with sensible defaults for tests."""
    defaults: dict[str, Any] = {
        "id": "test-1",
        "event": "Severe Thunderstorm Warning",
        "msg_type": "Alert",
        "severity": "Severe",
        "headline": "headline",
        "description": "body",
        "area_desc": "Somewhere",
        "expires": "2099-01-01T00:00:00+00:00",
        "provider": "nws",
    }
    defaults.update(overrides)
    return CAPAlert(**defaults)


@pytest.fixture
def alert_factory():
    return make_alert
