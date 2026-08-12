"""The update listener owns every reload decision.

Home Assistant deprecated pairing a config-entry update listener with a
reloading config-flow method in 2026.6 — it reloads twice and can race — and
makes it an error in 2026.12. The reconfigure flow therefore calls
``async_update_and_abort``, which does *not* reload, so any change that used to
rely on the flow's reload has to be recognised by this listener instead.

The listener is kept (rather than dropping it and reloading unconditionally,
the other sanctioned migration) because reloading tears down the ECCC NAAD
stream socket, which must not happen for a scan-interval tweak.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.cap_alerts import _async_entry_updated
from custom_components.cap_alerts.coordinator import AlertsDataUpdateCoordinator


class _Reloads:
    """Records reload requests in place of hass.config_entries."""

    def __init__(self) -> None:
        self.reloaded: list[str] = []

    def async_schedule_reload(self, entry_id: str) -> None:
        self.reloaded.append(entry_id)


class _Hass:
    def __init__(self) -> None:
        self.config_entries = _Reloads()


class _Coordinator:
    """Stands in for a live coordinator; only the listener's inputs matter."""

    def __init__(self, entry_data: dict, streaming: bool = False) -> None:
        self._entry_data = dict(entry_data)
        self._streaming = streaming
        self.update_interval = None
        self.timeout: int | None = None
        self.refreshed = False

    # Real implementations, exercised as-is.
    entry_data_changed = AlertsDataUpdateCoordinator.entry_data_changed

    @property
    def streaming(self) -> bool:
        return self._streaming

    def resolve_update_interval(self, entry) -> str:
        return "interval"

    def update_timeout(self, timeout: int) -> None:
        self.timeout = timeout

    async def async_request_refresh(self) -> None:
        self.refreshed = True


def _entry(data: dict, options: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(entry_id="e1", data=dict(data), options=dict(options or {}))


# ---------------------------------------------------------------------------
# Reconfigure — the case the flow no longer reloads for
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_changed_entry_data_reloads():
    """Without this the reconfigure would appear to work and change nothing."""
    hass = _Hass()
    coordinator = _Coordinator({"provider": "wmo", "source_id": "cn-cma-xx"})
    entry = _entry({"provider": "wmo", "source_id": "ph-pagasa-en"})
    entry.runtime_data = coordinator

    await _async_entry_updated(hass, entry)

    assert hass.config_entries.reloaded == ["e1"]
    assert not coordinator.refreshed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("before", "after"),
    [
        # Every reconfigure shape across the providers.
        (
            {"provider": "nws", "zone_id": "OHC049"},
            {"provider": "nws", "zone_id": "MOC217"},
        ),
        (
            {"provider": "eccc", "province": "ON"},
            {"provider": "eccc", "province": "BC"},
        ),
        (
            {"provider": "wmo", "source_id": "x-y-z"},
            {"provider": "wmo", "gps_loc": "1,2"},
        ),
        # Switching provider entirely.
        (
            {"provider": "nws", "zone_id": "OHC049"},
            {"provider": "eccc", "province": "ON"},
        ),
        # A filter-mode switch that only *removes* a key.
        (
            {"provider": "wmo", "source_id": "s", "gps_loc": "1,2"},
            {"provider": "wmo", "source_id": "s"},
        ),
    ],
)
async def test_every_reconfigure_shape_reloads(before: dict, after: dict):
    hass = _Hass()
    coordinator = _Coordinator(before)
    entry = _entry(after)
    entry.runtime_data = coordinator

    await _async_entry_updated(hass, entry)

    assert hass.config_entries.reloaded == ["e1"]


# ---------------------------------------------------------------------------
# Options — must stay in place, or a scan-interval tweak drops the NAAD socket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unchanged_data_applies_options_in_place():
    """A live ECCC streaming entry: the exact case a reload would damage."""
    hass = _Hass()
    data = {"provider": "eccc", "province": "ON"}
    coordinator = _Coordinator(data, streaming=True)  # streaming defaults on
    entry = _entry(data, {"scan_interval": 600, "timeout": 45})
    entry.runtime_data = coordinator

    await _async_entry_updated(hass, entry)

    assert hass.config_entries.reloaded == []
    assert coordinator.timeout == 45
    assert coordinator.update_interval == "interval"
    assert coordinator.refreshed


@pytest.mark.asyncio
async def test_geocode_prefix_option_does_not_reload():
    """It is read per-poll in _apply, so it needs no rebuild."""
    hass = _Hass()
    data = {"provider": "wmo", "source_id": "cn-cma-xx"}
    coordinator = _Coordinator(data)
    entry = _entry(data, {"geocode_prefixes": ["13"]})
    entry.runtime_data = coordinator

    await _async_entry_updated(hass, entry)

    assert hass.config_entries.reloaded == []
    assert coordinator.refreshed


# ---------------------------------------------------------------------------
# Streaming toggle — pre-existing reload case, still owned by the listener
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("was_streaming", [True, False])
async def test_streaming_toggle_reloads(was_streaming: bool):
    hass = _Hass()
    data = {"provider": "eccc", "province": "ON"}
    coordinator = _Coordinator(data, streaming=was_streaming)
    entry = _entry(data, {"streaming": not was_streaming})
    entry.runtime_data = coordinator

    await _async_entry_updated(hass, entry)

    assert hass.config_entries.reloaded == ["e1"]


@pytest.mark.asyncio
async def test_data_change_reloads_once_even_if_streaming_also_changed():
    """Both branches reload; the entry must not be scheduled twice."""
    hass = _Hass()
    coordinator = _Coordinator({"provider": "eccc", "province": "ON"}, streaming=True)
    entry = _entry({"provider": "eccc", "province": "BC"}, {"streaming": False})
    entry.runtime_data = coordinator

    await _async_entry_updated(hass, entry)

    assert hass.config_entries.reloaded == ["e1"]


# ---------------------------------------------------------------------------
# The flow must not reload on its own — that pairing is what breaks in 2026.12
# ---------------------------------------------------------------------------


def test_config_flow_uses_the_non_reloading_update():
    from pathlib import Path

    pkg = Path(__file__).resolve().parent.parent / "custom_components" / "cap_alerts"
    modules = [pkg / "config_flow.py", *sorted((pkg / "flows").glob("*.py"))]
    assert len(modules) > 1, "flow step modules not found"
    source = "\n".join(path.read_text(encoding="utf-8") for path in modules)

    assert "async_update_reload_and_abort" not in source, (
        "async_update_reload_and_abort together with an update listener reloads "
        "twice and is an error from HA 2026.12 — use async_update_and_abort and "
        "let _async_entry_updated decide"
    )
    assert "async_update_and_abort" in source
