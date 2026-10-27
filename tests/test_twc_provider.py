"""Tests for the U.S. Tsunami Warning Centers provider (#298).

Fixtures are live captures from tsunami.gov, unedited: NTWC's 2025-09-18
Komandorski sequence on PAAQWEAK51 (Advisory, its Update, the Cancellation),
PTWC's first Hawaii bulletin for the 2025-07-29 Kamchatka event (a Watch with
35 UGC zones), a Hawaii information statement from 2026-10-25 and its slot
JSON (which is not valid JSON), and the final Pacific threat bulletin for the
2026-10-24 Vanuatu event (an Update with no ``<info>``).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.cap_alerts.const import (
    CONF_PROVINCE,
    REMOVAL_REASON_ENDED,
    TWC_BASE_URL,
    TWC_REGIONS,
    TWC_SLOT_URL,
)
from custom_components.cap_alerts.conventions import conventions_for
from custom_components.cap_alerts.model import CAPAlert
from custom_components.cap_alerts.normalize import normalize_alerts
from custom_components.cap_alerts.providers import ProviderError
from custom_components.cap_alerts.providers.cap import parse_cap_alert
from custom_components.cap_alerts.providers.twc import (
    TWCProvider,
    compute_twc_id,
    event_dir_from_slot,
)
from custom_components.cap_alerts.store import AlertStore
from tests.conftest import StubSession

_FIXTURES = Path(__file__).parent / "fixtures"

_HW40_DIR = "/events/PHEB/2025/07/29/25210002/1/WEHW40/"
_HW42_DIR = "/events/PHEB/2026/10/24/26297050/1/WEHW42/"
_AK51_DIRS = (
    "/events/PAAQ/2025/09/18/t2ssp5/1/WEAK51/",
    "/events/PAAQ/2025/09/18/t2ssp5/2/WEAK51/",
    "/events/PAAQ/2025/09/18/t2ssp5/3/WEAK51/",
)


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _slot_url(slot: str) -> str:
    return TWC_SLOT_URL.format(slot=slot)


def _slot_body(event_dir: str) -> str:
    return json.dumps({"eventDir": event_dir, "bulletinNr": "1"})


def _cap_url(event_dir: str) -> str:
    twc = event_dir.split("/")[2]
    return f"{TWC_BASE_URL}{event_dir}{twc}CAP.xml"


def _far_future(xml: str) -> str:
    """The bulletin with its ``expires`` moved out of the past, so it is live."""
    doc = parse_cap_alert(xml)
    assert doc is not None
    return xml.replace(doc.infos[0].expires, "2099-01-01T00:00:00-00:00")


def _hawaii(**overrides: Any) -> dict[str, Any]:
    responses: dict[str, Any] = {
        _slot_url("PHEBWEHW40"): _slot_body(_HW40_DIR),
        _cap_url(_HW40_DIR): _fixture("twc_wehw40_watch.xml"),
        _slot_url("PHEBWEHW42"): _fixture("twc_slot_phebwehw42.json"),
        _cap_url(_HW42_DIR): _fixture("twc_wehw42_statement.xml"),
    }
    responses.update(overrides)
    return responses


async def _fetch(
    session: StubSession,
    region: str = "HI",
    provider: TWCProvider | None = None,
) -> list[CAPAlert]:
    return await (provider or TWCProvider()).async_fetch(
        session,  # type: ignore[arg-type]
        {CONF_PROVINCE: region},
        {},
        user_agent="test",
    )


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------


def test_event_dir_read_from_a_slot_that_is_not_json() -> None:
    """PHEBWEHW42 writes ``"eventLat" : 28.,``; the pattern still finds the dir."""
    text = _fixture("twc_slot_phebwehw42.json")
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)
    assert event_dir_from_slot(text) == _HW42_DIR


def test_event_dir_absent() -> None:
    assert event_dir_from_slot('{"TWCID" : "PHEB"}') is None


def test_every_region_has_a_warning_and_an_information_slot() -> None:
    for _label, slots in TWC_REGIONS.values():
        assert len(slots) == 2
        assert all(slot[:4] in ("PAAQ", "PHEB") for slot in slots)


async def test_region_reads_both_slots() -> None:
    session = StubSession(_hawaii())
    alerts = await _fetch(session)

    assert [a.event for a in alerts] == ["Tsunami Watch", "Tsunami Information"]
    watch, statement = alerts
    assert watch.provider == statement.provider == "twc"
    assert watch.url == _cap_url(_HW40_DIR)
    assert len(watch.geocodes["UGC"]) == 35
    assert statement.geocodes == {}
    # The statement's only shape is the epicenter; nothing is drawn.
    assert watch.geometry is None and statement.geometry is None
    assert statement.parameters is not None
    assert "EventLatLon" in statement.parameters
    assert all(h == {"User-Agent": "test"} for h in session.request_headers)


async def test_scope_needs_no_check() -> None:
    """The region is a closed dropdown; there is nothing to ask upstream."""
    session = StubSession({})
    provider = TWCProvider()
    assert await provider.async_validate_config(session, {CONF_PROVINCE: "HI"}) is None  # type: ignore[arg-type]
    assert session.requested == []


async def test_unknown_region_raises() -> None:
    with pytest.raises(ProviderError, match="unknown region"):
        await _fetch(StubSession({}), region="XX")


async def test_never_issued_slot_is_empty() -> None:
    """A product's slot 404s until it is first issued (PAAQWEXX30 today)."""
    session = StubSession(_hawaii(**{_slot_url("PHEBWEHW40"): None}))
    alerts = await _fetch(session)
    assert [a.event for a in alerts] == ["Tsunami Information"]


async def test_failed_slot_fails_the_poll() -> None:
    session = StubSession(_hawaii(**{_slot_url("PHEBWEHW40"): (503, "")}))
    with pytest.raises(ProviderError, match="slot HTTP 503"):
        await _fetch(session)


async def test_slot_without_event_dir_fails_the_poll() -> None:
    session = StubSession(_hawaii(**{_slot_url("PHEBWEHW40"): "<html></html>"}))
    with pytest.raises(ProviderError, match="no eventDir"):
        await _fetch(session)


async def test_unreadable_bulletin_skips_its_slot() -> None:
    """The store retains what the slot held; failing the poll would not help."""
    session = StubSession(
        _hawaii(**{_cap_url(_HW40_DIR): (500, ""), _cap_url(_HW42_DIR): "not xml"})
    )
    assert await _fetch(session) == []


async def test_unchanged_slot_is_revalidated_and_its_bulletin_reused() -> None:
    modified = {"Last-Modified": "Sat, 25 Oct 2026 02:26:21 GMT"}
    slot = _fixture("twc_slot_phebwehw42.json")
    session = StubSession(
        _hawaii(
            **{
                _slot_url("PHEBWEHW40"): None,
                _slot_url("PHEBWEHW42"): [(200, slot, modified), (304, "")],
            }
        )
    )
    provider = TWCProvider()
    first = await _fetch(session, provider=provider)
    second = await _fetch(session, provider=provider)

    assert first == second
    assert session.requested.count(_cap_url(_HW42_DIR)) == 1
    last_slot_read = max(
        i for i, url in enumerate(session.requested) if url == _slot_url("PHEBWEHW42")
    )
    headers = session.request_headers[last_slot_read]
    assert headers is not None
    assert headers["If-Modified-Since"] == modified["Last-Modified"]


async def test_moved_slot_fetches_the_new_bulletin() -> None:
    session = StubSession(
        {
            _slot_url("PAAQWEAK51"): [_slot_body(d) for d in _AK51_DIRS[:2]],
            _cap_url(_AK51_DIRS[0]): _fixture("twc_weak51_advisory.xml"),
            _cap_url(_AK51_DIRS[1]): _fixture("twc_weak51_update.xml"),
        }
    )
    provider = TWCProvider()
    (first,) = await _fetch(session, "AK", provider)
    (second,) = await _fetch(session, "AK", provider)
    assert first.msg_type == "Alert" and second.msg_type == "Update"


# ---------------------------------------------------------------------------
# Identity and lifecycle
# ---------------------------------------------------------------------------


def test_one_entity_across_a_products_bulletins() -> None:
    """Advisory → Update → Cancellation is one incident on one product."""
    ids = set()
    for name in ("advisory", "update", "cancellation"):
        doc = parse_cap_alert(_fixture(f"twc_weak51_{name}.xml"))
        assert doc is not None
        ids.add(compute_twc_id("PAAQWEAK51", doc))
    assert len(ids) == 1


def test_same_event_on_two_products_is_two_entities() -> None:
    doc = parse_cap_alert(_fixture("twc_wehw42_statement.xml"))
    assert doc is not None
    assert compute_twc_id("PHEBWEHW42", doc) != compute_twc_id("PHEBWEGM42", doc)


async def test_identifier_and_references_carry_the_product() -> None:
    """PTWC reuses ``PHEB-{n}-{eventid}`` across every product of one event."""
    session = StubSession(
        {
            _slot_url("PAAQWEAK51"): _slot_body(_AK51_DIRS[1]),
            _cap_url(_AK51_DIRS[1]): _fixture("twc_weak51_update.xml"),
        }
    )
    (alert,) = await _fetch(session, "AK")
    assert alert.identifier == "PAAQ-2-t2ssp5/WEAK51"
    assert alert.references == ("PAAQ-1-t2ssp5/WEAK51",)


async def test_cancellation_ends_the_alert() -> None:
    session = StubSession(
        {
            _slot_url("PAAQWEAK51"): _slot_body(_AK51_DIRS[2]),
            _cap_url(_AK51_DIRS[2]): _fixture("twc_weak51_cancellation.xml"),
        }
    )
    (alert,) = await _fetch(session, "AK")
    assert alert.msg_type == "Update"
    assert alert.lifecycle_status == "cancelled"
    # Still inside its expires, it is an early end: cancel, not expired.
    live = replace(alert, expires="2099-01-01T00:00:00+00:00")
    (normalized,) = normalize_alerts([live])
    assert normalized.phase == "cancel"
    reasons = conventions_for("twc").lifecycle_removal_reasons
    assert reasons[alert.lifecycle_status] == REMOVAL_REASON_ENDED


async def test_bulletin_with_no_info_ends_the_alert() -> None:
    """A threat sequence ends on an Update carrying no ``<info>`` at all."""
    event_dir = "/events/PHEB/2026/10/24/26297000/2/WEPA40/"
    session = StubSession(
        _hawaii(
            **{
                _slot_url("PHEBWEHW40"): _slot_body(event_dir),
                _cap_url(event_dir): _fixture("twc_wepa40_final.xml"),
                _slot_url("PHEBWEHW42"): None,
            }
        )
    )
    (alert,) = await _fetch(session)
    assert alert.lifecycle_status == "cancelled"
    (normalized,) = normalize_alerts([alert])
    assert normalized.phase == "cancel"


async def test_live_bulletins_normalize_on_cap_severity() -> None:
    session = StubSession(
        _hawaii(
            **{
                _cap_url(_HW40_DIR): _far_future(_fixture("twc_wehw40_watch.xml")),
                _cap_url(_HW42_DIR): _far_future(_fixture("twc_wehw42_statement.xml")),
            }
        )
    )
    watch, statement = normalize_alerts(await _fetch(session))
    assert (watch.phase, watch.severity_normalized) == ("new", "moderate")
    assert (statement.phase, statement.severity_normalized) == ("new", "minor")
    assert watch.icon == statement.icon == "mdi:tsunami"


def test_row_hides_area_code_narrowing() -> None:
    """Statements and cancellations carry no geocodes (#298)."""
    assert conventions_for("twc").publishes_geocodes is False


async def test_shared_identifier_does_not_cross_products() -> None:
    """Two products, one raw identifier: a revision of one never ends the other.

    PHEBWEHW42, PHEBWEGM42 and PHEBWEZS42 were all ``PHEB-1-26297050`` on
    2026-10-25. Here the region's two slots hold that same bulletin; then the
    first moves to bulletin 2, which references bulletin 1, and the second
    goes quiet. Unqualified, the store would read the reference as replacing
    the second product's alert and drop it without an ``incident_removed``.
    It has to be retained instead, as any alert missing before its expiry is.
    """
    statement = _far_future(_fixture("twc_wehw42_statement.xml"))
    doc = parse_cap_alert(statement)
    assert doc is not None
    revision = statement.replace(
        "<identifier>PHEB-1-26297050</identifier>",
        "<identifier>PHEB-2-26297050</identifier>"
        f"<references>ntwc@noaa.gov,PHEB-1-26297050,{doc.sent}</references>",
    ).replace("<msgType>Alert</msgType>", "<msgType>Update</msgType>")
    dir_2 = _HW40_DIR.replace("/1/", "/2/")
    session = StubSession(
        {
            _slot_url("PHEBWEHW40"): [_slot_body(_HW40_DIR), _slot_body(dir_2)],
            _cap_url(_HW40_DIR): statement,
            _cap_url(dir_2): revision,
            _slot_url("PHEBWEHW42"): [_slot_body(_HW42_DIR), (404, "")],
            _cap_url(_HW42_DIR): statement,
        }
    )
    provider = TWCProvider()
    store = AlertStore(
        "entry1", "twc", fire=MagicMock(), lookup_entity_id=lambda _id: None
    )

    first = store.process(normalize_alerts(await _fetch(session, provider=provider)))
    assert len(first) == 2
    other = next(a for a in first if a.identifier.endswith("/WEHW42"))

    second = store.process(normalize_alerts(await _fetch(session, provider=provider)))
    by_id = {a.id: a for a in second}
    assert other.id in by_id
    assert by_id[other.id].stale is True
