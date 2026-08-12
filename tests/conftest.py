"""Shared test fixtures."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Runs before the integration is imported, here and in every test file.
# ``custom_components.cap_alerts`` must resolve to the real package so HA's
# integration loader can set up config entries in the lifecycle tests. The
# plugin ships its own ``testing_config/custom_components`` REGULAR package,
# which shadows the repo's namespace package regardless of sys.path order, and
# HA's loader discovers integrations by iterating ``custom_components.__path__``
# — so extend that path with the repo's directory.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
import custom_components  # noqa: E402

_repo_cc = str(_REPO_ROOT / "custom_components")
if _repo_cc not in custom_components.__path__:
    custom_components.__path__.append(_repo_cc)

from custom_components.cap_alerts.model import CAPAlert  # noqa: E402


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
    - ``list``: a per-call sequence of any of the above; successive GETs of the
      same URL consume the next element, and the last element repeats once the
      sequence is exhausted (models retry/transient-failure scenarios)
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self._seq_index: dict[str, int] = {}
        self.requested: list[str] = []

    def get(self, url: str, **kwargs: Any) -> Any:
        self.requested.append(url)
        value = self._responses.get(url)
        if isinstance(value, list):
            idx = min(self._seq_index.get(url, 0), len(value) - 1) if value else 0
            self._seq_index[url] = self._seq_index.get(url, 0) + 1
            value = value[idx] if value else None
        return self._materialize(value)

    @staticmethod
    def _materialize(value: Any) -> Any:
        if value is None:
            return _StubResponse(404, "")
        if callable(value):
            return _ErrorContext(value())
        if isinstance(value, tuple):
            status, body = value
            return _StubResponse(status, body)
        return _StubResponse(200, str(value))
