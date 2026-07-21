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


def _ensure_ha_stubs() -> None:
    """Pre-stub homeassistant modules so provider tests can import without HA."""
    if "homeassistant" in sys.modules:
        return

    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    helpers = types.ModuleType("homeassistant.helpers")
    uc_mod = types.ModuleType("homeassistant.helpers.update_coordinator")
    er_mod = types.ModuleType("homeassistant.helpers.entity_registry")

    class HomeAssistant:  # noqa: D401 — stub
        pass

    class UpdateFailed(Exception):
        pass

    core.HomeAssistant = HomeAssistant
    uc_mod.UpdateFailed = UpdateFailed
    er_mod.async_get = lambda hass: hass.entity_registry
    ha.helpers = helpers
    helpers.update_coordinator = uc_mod
    helpers.entity_registry = er_mod

    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.update_coordinator"] = uc_mod
    sys.modules["homeassistant.helpers.entity_registry"] = er_mod


_ensure_ha_stubs()

# When pytest-homeassistant-custom-component is installed, its plugin imports
# the real ``homeassistant`` package before this conftest runs, so
# ``_ensure_ha_stubs`` no-ops. Distinguish that from our stub module, which
# was created with ``types.ModuleType`` and therefore has no ``__file__``.
_REAL_HA = getattr(sys.modules["homeassistant"], "__file__", None) is not None

# Pre-load submodules so tests can import ``cap_alerts.*`` without executing
# the HA-dependent package __init__.
_model = _load_submodule("model")
_icons = _load_submodule("icons")
_normalize = _load_submodule("normalize")

if _REAL_HA:
    # ``custom_components.cap_alerts`` must resolve to the REAL package so
    # HA's integration loader can set up config entries in lifecycle tests
    # (a synthetic alias module has no async_setup_entry). The plugin ships
    # its own ``testing_config/custom_components`` REGULAR package, which
    # shadows our namespace package regardless of sys.path order, and HA's
    # loader discovers integrations by iterating ``custom_components.__path__``
    # — so extend that path with the repo's directory.
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    import custom_components

    _repo_cc = str(_REPO_ROOT / "custom_components")
    if _repo_cc not in custom_components.__path__:
        custom_components.__path__.append(_repo_cc)
else:
    # Stub mode (no plugin): alias ``custom_components.cap_alerts.*`` to the
    # pre-loaded copies so imports work without real HA.
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


# ---------------------------------------------------------------------------
# Stub HTTP session for provider tests
# ---------------------------------------------------------------------------


class _StubResponse:
    """Minimal aiohttp-compatible response for testing."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def json(self, **kwargs: Any) -> Any:
        import json

        return json.loads(self._body)

    async def __aenter__(self) -> "_StubResponse":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass


class _ErrorContext:
    """Context manager that raises an exception on enter."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> None:
        raise self._exc

    async def __aexit__(self, *args: Any) -> None:
        pass


class StubSession:
    """Stub aiohttp ClientSession for hermetic tests.

    ``responses`` maps URL → one of:
    - ``str``: body with status 200
    - ``(int, str)``: explicit (status, body)
    - ``callable``: zero-arg factory; the returned exception is raised on enter
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.requested: list[str] = []

    def get(self, url: str, **kwargs: Any) -> Any:
        self.requested.append(url)
        value = self._responses.get(url)
        if value is None:
            return _StubResponse(404, "")
        if callable(value):
            return _ErrorContext(value())
        if isinstance(value, tuple):
            status, body = value
            return _StubResponse(status, body)
        return _StubResponse(200, str(value))
