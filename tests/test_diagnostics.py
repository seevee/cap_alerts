"""Config-entry diagnostics — the support artifact (issue #134).

Two things are being pinned here. The payload has to *answer* a report: which
source, which query, when it last worked, which convention row matched. And it
has to be safe to paste into a public issue, which means no coordinates and no
tracker entity id anywhere in the serialized bytes — not in entry data, not in
the resolved data a tracker produced, and not inside a provider URL built from
either.

Most tests drive the payload builder against a stub coordinator, since what is
under test is the rendering rather than the fetch. One goes through Home
Assistant's own download view, which is the only way to catch a payload that
builds fine and then fails to serialize.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.cap_alerts import diagnostics
from custom_components.cap_alerts.const import (
    CONF_COUNTRY,
    CONF_COUNTRY_ENTITY,
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_PROVIDER,
    CONF_PROVINCE,
    CONF_TRACKER_ENTITY,
    CONF_ZONE_ID,
    NAAD_STREAM_HOST,
)
from custom_components.cap_alerts.conventions import FMI_SENDER, METEOFRANCE_SENDER
from custom_components.cap_alerts.coordinator import AlertsDataUpdateCoordinator
from tests.conftest import make_alert

DOMAIN = "cap_alerts"
FEED = "https://rss.alertready.ca/"
REDACTED = "**REDACTED**"


class _StubCoordinator:
    """Everything ``async_get_config_entry_diagnostics`` reads off a coordinator.

    A real one needs a provider, a store and a running HA instance; none of
    that changes what the payload looks like.
    """

    def __init__(
        self,
        *,
        alerts=None,
        resolved_config=None,
        resolved_options=None,
        last_success=None,
        last_failure=None,
        failure_error=None,
        success=True,
        streaming=False,
        connected=False,
        live_documents=0,
        last_backfill=None,
        interval_seconds=300,
    ) -> None:
        self.data = {a.id: a for a in (alerts or [])}
        self._resolved_config = resolved_config
        self._resolved_options = resolved_options
        self.last_update_success = success
        self.last_update_success_time = last_success
        self.last_update_failure_time = last_failure
        self.last_update_failure = failure_error
        self.update_interval = timedelta(seconds=interval_seconds)
        self.streaming = streaming
        self.stream_connected = connected
        self.live_doc_count = live_documents
        self.last_backfill_time = last_backfill
        self._entry = None

    def bind(self, entry) -> "_StubCoordinator":
        self._entry = entry
        return self

    @property
    def resolved_config(self):
        if self._resolved_config is None:
            return self._entry.data
        return self._resolved_config

    @property
    def resolved_options(self):
        if self._resolved_options is None:
            return self._entry.options
        return self._resolved_options


def _entry(data, options=None, **coordinator_kwargs) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=data, options=options or {})
    entry.runtime_data = _StubCoordinator(**coordinator_kwargs).bind(entry)
    return entry


async def _payload(entry) -> dict:
    return await diagnostics.async_get_config_entry_diagnostics(None, entry)


# ---------------------------------------------------------------------------
# What the payload answers
# ---------------------------------------------------------------------------


async def test_reports_provider_scope_and_endpoint():
    entry = _entry(
        {CONF_PROVIDER: "nws", CONF_ZONE_ID: "OHZ049"},
        last_success=datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
    )

    payload = await _payload(entry)

    assert payload["entry"]["provider"] == "nws"
    assert payload["entry"]["scope"] == {"mode": "zone", "value": "OHZ049"}
    assert payload["source"]["endpoints"] == [
        "https://api.weather.gov/alerts/active?zone=OHZ049",
        "https://api.weather.gov/alerts?message_type=cancel&zone=OHZ049",
    ]
    assert payload["update"]["last_success"] == "2026-08-12T10:00:00+00:00"
    assert payload["update"]["interval_seconds"] == 300


async def test_reports_the_failure_that_a_recovery_did_not_erase():
    """A dump is read after the fact, so the last failure has to outlive it."""
    entry = _entry(
        {CONF_PROVIDER: "nws", CONF_ZONE_ID: "OHZ049"},
        success=True,
        last_success=datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
        last_failure=datetime(2026, 8, 12, 4, 12, tzinfo=timezone.utc),
        failure_error="nws: timeout after 30s",
    )

    update = (await _payload(entry))["update"]

    assert update["success"] is True
    assert update["last_failure"] == "2026-08-12T04:12:00+00:00"
    assert update["last_failure_error"] == "nws: timeout after 30s"


async def test_reports_eccc_feed_source_and_both_union_hosts():
    entry = _entry({CONF_PROVIDER: "eccc", CONF_PROVINCE: "ON"})

    payload = await _payload(entry)

    assert payload["source"]["feed_source"] == "auto"
    assert len(payload["source"]["endpoints"]) == 2
    assert any("alertready" in url for url in payload["source"]["endpoints"])


async def test_reports_the_pinned_host_when_the_feed_source_is_named():
    entry = _entry(
        {CONF_PROVIDER: "eccc", CONF_PROVINCE: "ON"},
        {"feed_source": "pelmorex"},
    )

    payload = await _payload(entry)

    assert payload["source"]["feed_source"] == "pelmorex"
    assert len(payload["source"]["endpoints"]) == 1
    assert "pelmorex" in payload["source"]["endpoints"][0]


async def test_reports_stream_state_for_a_streaming_entry():
    entry = _entry(
        {CONF_PROVIDER: "eccc", CONF_PROVINCE: "ON"},
        streaming=True,
        connected=True,
        live_documents=42,
        last_backfill=datetime(2026, 8, 12, 9, 30, tzinfo=timezone.utc),
    )

    stream = (await _payload(entry))["stream"]

    assert stream["enabled"] is True
    assert stream["connected"] is True
    assert stream["endpoint"] == f"{NAAD_STREAM_HOST}:8443"
    assert stream["live_documents"] == 42
    assert stream["last_backfill"] == "2026-08-12T09:30:00+00:00"


async def test_stream_block_is_present_and_empty_for_a_polling_entry():
    """Streaming being off is an answer; the block should not just vanish."""
    entry = _entry({CONF_PROVIDER: "nws", CONF_ZONE_ID: "OHZ049"})

    stream = (await _payload(entry))["stream"]

    assert stream["enabled"] is False
    assert stream["endpoint"] is None


async def test_reports_configured_and_resolved_language():
    entry = _entry(
        {CONF_PROVIDER: "eccc", CONF_PROVINCE: "QC"},
        {CONF_LANGUAGE: "auto"},
        resolved_options={CONF_LANGUAGE: "fr-CA"},
    )

    language = (await _payload(entry))["filters"]["language"]

    assert language == {"configured": "auto", "resolved": "fr-CA"}


async def test_reports_the_active_filters():
    entry = _entry(
        {CONF_PROVIDER: "nws", CONF_ZONE_ID: "OHZ049"},
        {"exclude_marine": True, "geocode_prefixes": ["OHZ", "OHC"]},
    )

    filters = (await _payload(entry))["filters"]

    assert filters["exclude_marine"] is True
    assert filters["geocode_prefixes"] == ["OHZ", "OHC"]


async def test_counts_split_active_from_upcoming():
    now = datetime.now(timezone.utc)
    entry = _entry(
        {CONF_PROVIDER: "nws", CONF_ZONE_ID: "OHZ049"},
        alerts=[
            make_alert(id="a", onset=(now - timedelta(hours=1)).isoformat()),
            make_alert(id="b", onset=(now + timedelta(hours=6)).isoformat()),
            make_alert(id="c", severity_normalized="extreme"),
        ],
    )

    alerts = (await _payload(entry))["alerts"]

    assert alerts["total"] == 3
    assert (alerts["active"], alerts["upcoming"]) == (2, 1)
    assert alerts["by_severity"]["extreme"] == 1


# ---------------------------------------------------------------------------
# Alert rows: lifecycle, not bodies
# ---------------------------------------------------------------------------


async def test_alert_rows_carry_lifecycle_and_omit_the_body():
    entry = _entry(
        {CONF_PROVIDER: "eccc", CONF_PROVINCE: "ON"},
        alerts=[
            make_alert(
                id="a",
                provider="eccc",
                description="a wall of text",
                instruction="take shelter",
                headline="Wind warning in effect",
                phase="active",
                lifecycle_status="ongoing",
                geometry_ref="entry:eccc:a",
                geometry={"type": "Polygon", "coordinates": []},
            )
        ],
    )

    row = (await _payload(entry))["alerts"]["entries"][0]

    assert row["id"] == "a"
    assert row["phase"] == "active"
    assert row["lifecycle_status"] == "ongoing"
    assert row["has_geometry"] is True
    assert not {"description", "instruction", "headline", "geometry"} & set(row)


async def test_alert_rows_are_capped_and_the_remainder_is_counted():
    entry = _entry(
        {CONF_PROVIDER: "gdacs"},
        alerts=[make_alert(id=f"a{i}", provider="gdacs") for i in range(130)],
    )

    alerts = (await _payload(entry))["alerts"]

    assert alerts["total"] == 130
    assert len(alerts["entries"]) == diagnostics.MAX_ALERT_ROWS
    assert alerts["truncated"] == 30


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


async def test_gps_coordinates_appear_nowhere_in_the_dump():
    entry = _entry({CONF_PROVIDER: "nws", CONF_GPS_LOC: "40.7128,-74.006"})

    payload = await _payload(entry)

    assert payload["entry"]["scope"] == {"mode": "gps", "value": REDACTED}
    assert payload["entry"]["data"][CONF_GPS_LOC] == REDACTED
    # The NWS query string is built from the location, so redacting the config
    # alone would still leak it through the endpoint.
    assert payload["source"]["endpoints"][0].endswith(f"point={REDACTED}")
    assert "40.7128" not in json.dumps(payload)


async def test_tracker_and_the_location_it_resolved_to_are_both_redacted():
    entry = _entry(
        {CONF_PROVIDER: "nws", CONF_TRACKER_ENTITY: "device_tracker.pixel"},
        resolved_config={
            CONF_PROVIDER: "nws",
            CONF_TRACKER_ENTITY: "device_tracker.pixel",
            CONF_GPS_LOC: "40.7128,-74.006",
        },
    )

    payload = await _payload(entry)
    serialized = json.dumps(payload)

    assert payload["entry"]["scope"] == {"mode": "tracker", "value": REDACTED}
    assert payload["entry"]["resolved"]["data"][CONF_GPS_LOC] == REDACTED
    assert "pixel" not in serialized
    assert "40.7128" not in serialized


async def test_country_source_mode_redacts_the_entity_but_keeps_the_country():
    """MeteoAlarm fully-mobile: the resolved country is the debuggable half."""
    entry = _entry(
        {
            CONF_PROVIDER: "meteoalarm",
            CONF_COUNTRY_ENTITY: "sensor.phone_country",
            CONF_TRACKER_ENTITY: "device_tracker.pixel",
        },
        resolved_config={
            CONF_PROVIDER: "meteoalarm",
            CONF_COUNTRY_ENTITY: "sensor.phone_country",
            CONF_TRACKER_ENTITY: "device_tracker.pixel",
            CONF_COUNTRY: "FR",
        },
    )

    payload = await _payload(entry)

    assert payload["entry"]["scope"] == {"mode": "country_source", "value": REDACTED}
    assert payload["entry"]["resolved"]["data"][CONF_COUNTRY] == "FR"
    assert payload["source"]["endpoints"] == [
        "https://feeds.meteoalarm.org/api/v1/warnings/feeds-france"
    ]
    assert "pixel" not in json.dumps(payload)


async def test_credentials_are_redacted_before_any_provider_needs_them():
    """No shipped provider authenticates; the wiring is here for the first that does."""
    entry = _entry(
        {CONF_PROVIDER: "wmo", "source_id": "in-imd-en", "api_key": "s3cr3t"},
        {"password": "hunter2", "access_token": "abc123"},
    )

    payload = await _payload(entry)
    serialized = json.dumps(payload)

    assert payload["entry"]["data"]["api_key"] == REDACTED
    assert payload["entry"]["options"]["password"] == REDACTED
    assert "s3cr3t" not in serialized
    assert "hunter2" not in serialized
    assert "abc123" not in serialized


async def test_an_entry_with_no_location_yet_reports_no_endpoint():
    """A half-configured NWS entry queries nothing, so it advertises nothing."""
    entry = _entry({CONF_PROVIDER: "nws"})

    assert (await _payload(entry))["source"]["endpoints"] == []


async def test_a_worldwide_entry_reports_a_scope_rather_than_a_gap():
    """GDACS with no GPS filter is fully configured, not half-configured."""
    entry = _entry({CONF_PROVIDER: "gdacs"})

    payload = await _payload(entry)

    assert payload["entry"]["scope"] == {"mode": "global", "value": None}
    assert len(payload["source"]["endpoints"]) == 2


# ---------------------------------------------------------------------------
# Convention rows
# ---------------------------------------------------------------------------


async def test_names_the_sender_dialect_row_and_the_senders_on_it():
    """The row that matched is the first thing a dialect report needs."""
    entry = _entry(
        {CONF_PROVIDER: "meteoalarm", CONF_COUNTRY: "FR"},
        alerts=[
            make_alert(id="a", provider="meteoalarm", sender=METEOFRANCE_SENDER),
            make_alert(id="b", provider="meteoalarm", sender="warnings@dwd.de"),
        ],
    )

    rows = {
        row["key"]: row
        for row in (await _payload(entry))["conventions"]["rows_in_effect"]
    }

    assert set(rows) == {f"meteoalarm/{METEOFRANCE_SENDER}", "meteoalarm"}
    dialect = rows[f"meteoalarm/{METEOFRANCE_SENDER}"]
    assert dialect["senders"] == [METEOFRANCE_SENDER]
    assert dialect["identity"] == "meteofrance_identity"
    assert dialect["keep"] == "meteofrance_is_live_warning"
    # DWD has no entry of its own, so it lands on the provider row.
    assert rows["meteoalarm"]["senders"] == ["warnings@dwd.de"]
    assert rows["meteoalarm"]["identity"] is None


async def test_episode_stages_are_reported_by_slot():
    entry = _entry(
        {CONF_PROVIDER: "meteoalarm", CONF_COUNTRY: "FI"},
        alerts=[make_alert(id="a", provider="meteoalarm", sender=FMI_SENDER)],
    )

    row = (await _payload(entry))["conventions"]["rows_in_effect"][0]

    assert [stage["slot"] for stage in row["stages"]] == ["explode", "merge"]
    assert all(stage["run"] for stage in row["stages"])


async def test_the_provider_row_is_reported_even_with_no_alerts_in_hand():
    """A quiet entry is exactly when a reporter asks what the entry is doing."""
    entry = _entry({CONF_PROVIDER: "nws", CONF_ZONE_ID: "OHZ049"})

    conventions = (await _payload(entry))["conventions"]

    assert conventions["rows_in_effect"] == []
    provider_row = conventions["provider_row"]
    assert provider_row["key"] == "nws"
    assert provider_row["severity"] == "nws_vtec_severity"
    assert provider_row["classifies_marine"] is True
    assert "AN" in provider_row["marine_code_prefixes"]
    assert provider_row["discovers_terminations"] is True


async def test_an_unknown_provider_degrades_to_an_empty_row():
    entry = _entry({CONF_PROVIDER: "bom"})

    provider_row = (await _payload(entry))["conventions"]["provider_row"]

    assert provider_row["key"] == "(none)"
    assert provider_row["severity"] is None
    assert provider_row["classifies_marine"] is False


# ---------------------------------------------------------------------------
# What the coordinator has to remember for any of the above to be reportable
# ---------------------------------------------------------------------------
#
# Built with ``object.__new__`` to skip the provider/store wiring these paths
# never touch, the way the resolution tests do.


def _bare_coordinator() -> AlertsDataUpdateCoordinator:
    coordinator = object.__new__(AlertsDataUpdateCoordinator)
    coordinator.last_update_failure = None
    coordinator.last_update_failure_time = None
    return coordinator


async def test_a_failed_update_is_stamped_and_re_raised_untouched():
    coordinator = _bare_coordinator()

    async def _boom():
        raise UpdateFailed("eccc: timeout after 30s")

    coordinator._async_fetch_data = _boom

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert coordinator.last_update_failure == "eccc: timeout after 30s"
    assert coordinator.last_update_failure_time is not None


async def test_a_silent_exception_is_recorded_by_type():
    """``str(err)`` is empty for plenty of exceptions; a blank field answers nothing."""
    coordinator = _bare_coordinator()

    async def _boom():
        raise TimeoutError

    coordinator._async_fetch_data = _boom

    with pytest.raises(TimeoutError):
        await coordinator._async_update_data()

    assert coordinator.last_update_failure == "TimeoutError"


def test_resolved_config_falls_back_to_entry_data_before_the_first_update():
    """Diagnostics is reachable on an entry whose first refresh has not landed."""
    coordinator = _bare_coordinator()
    coordinator.config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PROVIDER: "nws", CONF_ZONE_ID: "OHZ049"},
        options={CONF_LANGUAGE: "auto"},
    )

    assert coordinator.resolved_config[CONF_ZONE_ID] == "OHZ049"
    assert coordinator.resolved_options[CONF_LANGUAGE] == "auto"


# ---------------------------------------------------------------------------
# Through Home Assistant's own download view
# ---------------------------------------------------------------------------


def _cap_xml(identifier: str, event: str) -> str:
    now = datetime.now(timezone.utc)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        f"<identifier>{identifier}</identifier>"
        f"<sender>CWTO</sender><sent>{(now - timedelta(hours=1)).isoformat()}</sent>"
        "<status>Actual</status><msgType>Alert</msgType><scope>Public</scope>"
        "<info><language>en-CA</language><category>Met</category>"
        f"<event>{event}</event><urgency>Immediate</urgency>"
        "<severity>Moderate</severity><certainty>Likely</certainty>"
        f"<expires>{(now + timedelta(days=1)).isoformat()}</expires>"
        f"<headline>{event} in effect</headline><description>desc</description>"
        "<area><areaDesc>Ottawa</areaDesc>"
        "<polygon>45.0,-76.0 45.0,-75.5 45.5,-75.5 45.5,-76.0 45.0,-76.0</polygon>"
        "<geocode><valueName>profile:CAP-CP:Location:0.3</valueName>"
        "<value>3506008</value></geocode>"
        "</area></info></alert>"
    )


def _atom(cap_url: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:georss="http://www.georss.org/georss">'
        "<entry><id>atom-0</id><title>Warning</title>"
        '<category term="status=Actual"/>'
        "<georss:polygon>45.0 -76.0 45.0 -75.5 45.5 -75.5 45.5 -76.0 "
        "45.0 -76.0</georss:polygon>"
        f'<link type="application/cap+xml" href="{cap_url}"/>'
        "</entry></feed>"
    )


@pytest.mark.asyncio
async def test_download_view_serves_a_live_entry(
    hass, hass_client, aioclient_mock, enable_custom_integrations
):
    """End to end: platform discovered, payload JSON-serializable, alert present."""
    cap_url = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_url))
    aioclient_mock.get(cap_url, text=_cap_xml("urn:oid:A", "Wind Warning"))

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ECCC: Ontario",
        data={CONF_PROVIDER: "eccc", CONF_PROVINCE: "ON"},
        options={"streaming": False, "scan_interval": 300, "feed_source": "alertready"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    payload = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert payload["entry"]["provider"] == "eccc"
    assert payload["entry"]["scope"] == {"mode": "province", "value": "ON"}
    assert payload["source"]["endpoints"] == [FEED]
    assert payload["update"]["success"] is True
    assert payload["update"]["last_success"] is not None
    assert payload["entry"]["resolved"]["from_last_update"] is True
    assert payload["entry"]["resolved"]["options"][CONF_LANGUAGE] == "en-CA"
    assert payload["alerts"]["total"] == 1
    row = payload["alerts"]["entries"][0]
    assert row["identifier"] == "urn:oid:A"
    assert row["sender"] == "CWTO"
    assert row["geocodes"]["profile:CAP-CP:Location:0.3"] == ["3506008"]
    assert "description" not in row
    assert payload["conventions"]["rows_in_effect"][0]["key"] == "eccc"
