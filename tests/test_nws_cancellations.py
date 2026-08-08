"""NWS cancellation lookup — the explicit end-of-life signal the active feed omits.

Measured over a six-hour national window: NWS published 101 cancellations
(VTEC ``CAN`` / ``messageType=Cancel``) and **none** of them appeared on
``/alerts/active``, the endpoint the provider polls. Without this lookup a
cancelled warning is indistinguishable from a dropped record, so the store's
retain-on-absence rule holds it until its published expiry.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.test_nws_provider import _load_provider

_nws_mod = _load_provider("nws")
NWSProvider = _nws_mod.NWSProvider

CONFIG = {"zone_id": "OHC025"}


def _feature(
    tracking: str = "0042",
    msg_type: str = "Alert",
    expires: str = "2099-01-01T00:00:00+00:00",
) -> dict[str, Any]:
    """A VTEC-bearing feature. Identity keys on the VTEC event tuple, not the
    action, so a cancellation lands on the same alert id as the warning it ends.
    """
    action = {"Alert": "NEW", "Update": "CON", "Cancel": "CAN"}[msg_type]
    return {
        "properties": {
            "id": f"https://api.weather.gov/alerts/urn:oid:{tracking}.{action}",
            "event": "Severe Thunderstorm Warning",
            "messageType": msg_type,
            "severity": "Severe",
            "expires": expires,
            "parameters": {
                "VTEC": [f"/O.{action}.KOKX.SV.W.{tracking}.260414T1947Z-260414T2045Z/"]
            },
        }
    }


def _collection(*features: dict[str, Any]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": list(features)}


class _Pages:
    """Stands in for ``_fetch_page``, answering by URL."""

    def __init__(self, active: dict[str, Any], cancel: dict[str, Any] | None = None):
        self.active = active
        self.cancel = cancel
        self.urls: list[str] = []

    async def __call__(self, _session, url: str) -> dict[str, Any]:
        self.urls.append(url)
        if "message_type=cancel" in url:
            if self.cancel is None:
                raise _nws_mod.UpdateFailed("boom")
            return self.cancel
        return self.active

    @property
    def cancel_urls(self) -> list[str]:
        return [u for u in self.urls if "message_type=cancel" in u]


async def _fetch(provider, pages, monkeypatch):
    monkeypatch.setattr(provider, "_fetch_page", pages)
    return await provider.async_fetch(None, CONFIG, {})


@pytest.mark.asyncio
async def test_first_fetch_does_not_look_up_cancellations(monkeypatch):
    """Nothing is tracked yet, so there is nothing a cancellation could end."""
    provider = NWSProvider()
    pages = _Pages(active=_collection(_feature()))

    alerts = await _fetch(provider, pages, monkeypatch)

    assert len(alerts) == 1
    assert pages.cancel_urls == []


@pytest.mark.asyncio
async def test_cancellation_terminates_a_tracked_alert(monkeypatch):
    """The warning leaves the active feed; its cancellation arrives separately."""
    provider = NWSProvider()
    await _fetch(provider, _Pages(active=_collection(_feature())), monkeypatch)

    pages = _Pages(
        active=_collection(),
        cancel=_collection(_feature(msg_type="Cancel")),
    )
    alerts = await _fetch(provider, pages, monkeypatch)

    assert len(pages.cancel_urls) == 1
    assert "zone=OHC025" in pages.cancel_urls[0]
    # Same id as the warning, because VTEC identity ignores the action code —
    # so the store sees a terminal record for an alert it already tracks.
    assert len(alerts) == 1
    assert alerts[0].msg_type == "Cancel"


@pytest.mark.asyncio
async def test_cancellation_for_an_untracked_alert_is_dropped(monkeypatch):
    """A cancellation with no matching entity would fire an unpaired removal.

    The lookback window is wider than one cycle, so it returns cancellations for
    alerts this entry never had — filtered out as marine, or issued before the
    entry existed.
    """
    provider = NWSProvider()
    await _fetch(provider, _Pages(active=_collection(_feature("0042"))), monkeypatch)

    pages = _Pages(
        active=_collection(),
        cancel=_collection(_feature("9999", msg_type="Cancel")),
    )
    alerts = await _fetch(provider, pages, monkeypatch)

    assert alerts == []


@pytest.mark.asyncio
async def test_cancellation_ignored_while_the_alert_is_still_active(monkeypatch):
    """A partial cancellation: ended over one area, running on elsewhere.

    NWS does this — 1 of 174 terminal products in the sample had its VTEC
    tracking key still live on the active feed. There the active record is the
    truthful one and the cancellation must not retire the entity.
    """
    provider = NWSProvider()
    await _fetch(provider, _Pages(active=_collection(_feature())), monkeypatch)

    pages = _Pages(
        active=_collection(_feature(msg_type="Update")),
        cancel=_collection(_feature(msg_type="Cancel")),
    )
    alerts = await _fetch(provider, pages, monkeypatch)

    assert len(alerts) == 1
    assert alerts[0].msg_type == "Update"


@pytest.mark.asyncio
async def test_cancellation_failure_does_not_fail_the_poll(monkeypatch):
    """Losing the lookup costs a late termination, not an update."""
    provider = NWSProvider()
    await _fetch(provider, _Pages(active=_collection(_feature())), monkeypatch)

    pages = _Pages(active=_collection(_feature(msg_type="Update")), cancel=None)
    alerts = await _fetch(provider, pages, monkeypatch)

    assert len(alerts) == 1
    assert alerts[0].msg_type == "Update"


@pytest.mark.asyncio
async def test_cancellation_survives_a_failed_lookup_cycle(monkeypatch):
    """A failed lookup in the cycle the alert vanishes must not end discovery.

    The alert leaves the active feed and the cancellation lookup fails in the
    same cycle. The id stays eligible while the alert's expiry has not passed —
    the same window the store retains it for — so the next successful lookup
    still finds the CAN and terminates the alert, instead of holding it stale
    to its published expiry.
    """
    provider = NWSProvider()
    await _fetch(provider, _Pages(active=_collection(_feature())), monkeypatch)

    alerts = await _fetch(
        provider, _Pages(active=_collection(), cancel=None), monkeypatch
    )
    assert alerts == []

    pages = _Pages(
        active=_collection(),
        cancel=_collection(_feature(msg_type="Cancel")),
    )
    alerts = await _fetch(provider, pages, monkeypatch)

    assert len(pages.cancel_urls) == 1
    assert len(alerts) == 1
    assert alerts[0].msg_type == "Cancel"


@pytest.mark.asyncio
async def test_expired_id_leaves_the_eligible_set(monkeypatch):
    """Eligibility ends with the expiry, exactly when the store terminates.

    Once the alert's own expiry passes the store has already fired its
    removal, so a late CAN discovered after that would fire a second,
    unpaired ``incident_removed``. The id is pruned instead.
    """
    provider = NWSProvider()
    expired = _feature()
    expired["properties"]["expires"] = "2000-01-01T00:00:00+00:00"
    await _fetch(provider, _Pages(active=_collection(expired)), monkeypatch)

    # The alert vanishes; the lookup finds nothing. Its expiry is already past,
    # so the id is dropped from the eligible set here.
    await _fetch(
        provider, _Pages(active=_collection(), cancel=_collection()), monkeypatch
    )

    # A CAN surfacing later is not looked up at all: nothing is eligible.
    pages = _Pages(
        active=_collection(),
        cancel=_collection(_feature(msg_type="Cancel")),
    )
    alerts = await _fetch(provider, pages, monkeypatch)

    assert alerts == []
    assert pages.cancel_urls == []


@pytest.mark.asyncio
async def test_expiry_less_id_stays_eligible(monkeypatch):
    """An alert with no expiry keeps its id eligible, mirroring the store.

    ``store._retain_on_absence`` retains such an alert until an explicit
    terminal signal arrives, and for NWS that signal is a VTEC ``CAN`` this
    lookup is the only way to see. Ageing the id out on a missing field would
    pin the alert live permanently while disabling the one thing that could
    end it.
    """
    provider = NWSProvider()
    await _fetch(
        provider,
        _Pages(active=_collection(_feature(expires=""))),
        monkeypatch,
    )

    # Vanishes with nothing to age it out, and no cancellation yet.
    await _fetch(
        provider,
        _Pages(active=_collection(), cancel=_collection()),
        monkeypatch,
    )

    # Several quiet cycles later the cancellation finally surfaces.
    for _ in range(3):
        await _fetch(
            provider,
            _Pages(active=_collection(), cancel=_collection()),
            monkeypatch,
        )

    pages = _Pages(
        active=_collection(),
        cancel=_collection(_feature(msg_type="Cancel", expires="")),
    )
    alerts = await _fetch(provider, pages, monkeypatch)

    assert len(pages.cancel_urls) == 1
    assert len(alerts) == 1
    assert alerts[0].msg_type == "Cancel"


@pytest.mark.asyncio
async def test_eligible_set_is_bounded(monkeypatch):
    """Ids with no expiry never age out, so the carried-over set is capped."""
    provider = NWSProvider()
    cap = _nws_mod._MAX_CANCELLABLE_IDS

    # More expiry-less alerts than the cap, all of which then vanish.
    seeded = _collection(
        *(_feature(tracking=f"{i:04d}", expires="") for i in range(cap + 20))
    )
    await _fetch(provider, _Pages(active=seeded), monkeypatch)
    await _fetch(
        provider,
        _Pages(active=_collection(), cancel=_collection()),
        monkeypatch,
    )

    assert len(provider._tracked) == cap
