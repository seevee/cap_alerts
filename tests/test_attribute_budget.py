"""Bounding the attribute payload the recorder stores (issue #150).

The old per-field soft cap is gone, so these tests pin the two halves of what
replaced it: the byte-accurate truncation primitive, and the ladder that decides
which field pays when a serialized alert doesn't fit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers.json import json_bytes

from custom_components.cap_alerts.normalize import normalize_alerts
from custom_components.cap_alerts.payload import (
    PAYLOAD_BUDGET,
    RECORDER_CEILING,
    UNRECORDED_ATTRIBUTES,
    fit_to_budget,
    measure,
    truncate_bytes,
)

DOMAIN = "cap_alerts"
FEED = "https://rss.alertready.ca/"


# ---------------------------------------------------------------------------
# truncate_bytes
# ---------------------------------------------------------------------------


def test_under_limit_unchanged():
    assert truncate_bytes("short text", 4096) == "short text"


def test_empty_unchanged():
    assert truncate_bytes("", 4096) == ""


def test_over_limit_trimmed_with_ellipsis():
    out = truncate_bytes("a" * 4196, 4096)
    assert out.endswith("…")
    assert len(out.encode("utf-8")) <= 4096


def test_multibyte_utf8_respected():
    # Snowman is 3 bytes in UTF-8; a naive byte slice would land mid-character.
    out = truncate_bytes("☃" * 1400, 4096)
    encoded = out.encode("utf-8")
    assert len(encoded) <= 4096
    encoded.decode("utf-8")  # valid UTF-8, no mojibake
    assert out.endswith("…")


def test_limit_too_small_for_the_ellipsis():
    # No room for the marker itself: the marker wins, rather than a negative
    # slice quietly returning the head of the text untouched.
    assert truncate_bytes("some text", 2) == "…"


# ---------------------------------------------------------------------------
# measure
# ---------------------------------------------------------------------------


def test_measure_matches_the_recorders_encoding():
    attrs = {"id": "x", "event": "Wind Warning"}
    assert measure(attrs) == len(json_bytes(attrs))


def test_measure_skips_unrecorded_and_recorder_excluded_keys():
    attrs = {"id": "x", "parameters": {"blob": "P" * 5000}, "attribution": "A" * 500}
    assert measure(attrs) == len(json_bytes({"id": "x"}))


def test_measure_returns_none_when_the_payload_cannot_be_encoded():
    assert measure({"id": "x", "parameters": None, "blob": object()}) is None


# ---------------------------------------------------------------------------
# fit_to_budget — the ladder
# ---------------------------------------------------------------------------


def test_payload_under_budget_is_returned_untouched():
    attrs = {"id": "x", "description": "D" * 500}
    assert fit_to_budget(attrs) is attrs


def test_unrecorded_parameters_do_not_count_toward_the_budget():
    # 40 KB of provider parameters, and nothing is trimmed: the recorder never
    # measures them, so neither do we.
    attrs = {"id": "x", "description": "D" * 500, "parameters": {"p": "P" * 40000}}
    assert fit_to_budget(attrs) is attrs


def test_unmeasurable_payload_is_left_alone():
    attrs = {"id": "x", "description": "D" * 40000, "blob": object()}
    assert fit_to_budget(attrs) is attrs


def test_the_alternate_language_pays_first():
    attrs = {"id": "x", "description_alt": "A" * 1000, "description": "D" * 1000}
    out = fit_to_budget(attrs, budget=1600)

    assert out["description"] == "D" * 1000
    assert out["description_alt"].endswith("…")
    assert len(out["description_alt"]) < 1000
    assert measure(out) <= 1600


def test_a_field_trimmed_below_the_stub_floor_is_dropped_outright():
    # Only 140 bytes of description_alt would survive, which is a fragment
    # rather than text — so the key goes, and the primary is still untouched.
    attrs = {"id": "x", "description_alt": "A" * 1000, "description": "D" * 1000}
    out = fit_to_budget(attrs, budget=1200)

    assert "description_alt" not in out
    assert out["description"] == "D" * 1000
    assert measure(out) <= 1200


def test_both_alternates_are_spent_before_either_primary():
    attrs = {
        "id": "x",
        "description_alt": "A" * 2000,
        "instruction_alt": "B" * 2000,
        "description": "D" * 2000,
        "instruction": "I" * 2000,
    }
    out = fit_to_budget(attrs, budget=4200)

    assert "description_alt" not in out
    assert "instruction_alt" not in out
    assert out["description"] == "D" * 2000
    assert out["instruction"] == "I" * 2000
    assert measure(out) <= 4200


def test_the_instruction_outlives_the_description():
    attrs = {
        "id": "x",
        "description_alt": "A" * 2000,
        "instruction_alt": "B" * 2000,
        "description": "D" * 2000,
        "instruction": "I" * 2000,
    }
    out = fit_to_budget(attrs, budget=2600)

    # Both alternates gone, the description down to a fragment, and the
    # protective-action text still whole.
    assert "description_alt" not in out
    assert "instruction_alt" not in out
    assert out["description"].endswith("…")
    assert len(out["description"]) < 2000
    assert out["instruction"] == "I" * 2000
    assert measure(out) <= 2600


def test_structural_redundancy_goes_once_the_text_is_spent():
    attrs = {
        "id": "x",
        "description": "D" * 400,
        "affected_zones": ["ONZ001"] * 40,
        "affected_zone_uris": ["https://api.weather.gov/zones/forecast/ONZ001"] * 40,
        "geocodes": {"UGC": ["ONZ001"] * 40},
    }
    out = fit_to_budget(attrs, budget=900)

    # Text first, then the redundant structure — and only the redundant kind:
    # the URIs are a fixed prefix on codes that survive in ``affected_zones``.
    assert "description" not in out
    assert "affected_zone_uris" not in out
    assert out["geocodes"] == {"UGC": ["ONZ001"] * 40}
    assert out["affected_zones"] == ["ONZ001"] * 40
    assert measure(out) <= 900


def test_a_payload_that_cannot_be_made_to_fit_keeps_what_it_can():
    # Nothing on the ladder can rescue an alert whose bulk is a geocode
    # container this size, and the ladder does not carry a rung it hasn't got.
    # The expendable text still goes; the recorder reports the rest.
    attrs = {
        "id": "x",
        "description": "D" * 1000,
        "geocodes": {"UGC": [f"ONZ{i:03d}" for i in range(400)]},
    }
    out = fit_to_budget(attrs, budget=800)

    assert "description" not in out
    assert out["geocodes"] == attrs["geocodes"]
    assert measure(out) > 800


def test_the_input_dict_is_never_mutated():
    attrs = {"id": "x", "description_alt": "A" * 1000, "description": "D" * 1000}
    fit_to_budget(attrs, budget=1200)
    assert attrs["description_alt"] == "A" * 1000


# ---------------------------------------------------------------------------
# normalization no longer caps long text
# ---------------------------------------------------------------------------


def test_normalization_keeps_the_full_text_the_source_sent(alert_factory):
    # The cap moved to the payload, so the CAPAlert the store diffs against
    # carries what the feed published (issue #150).
    alert = alert_factory(description="D" * 10000, instruction="I" * 10000)
    (out,) = normalize_alerts([alert])
    assert out.description == "D" * 10000
    assert out.instruction == "I" * 10000


# ---------------------------------------------------------------------------
# End to end: a real entity's state fits under the recorder's ceiling
# ---------------------------------------------------------------------------


def _cap_xml(description: str, description_alt: str) -> str:
    now = datetime.now(timezone.utc)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        "<identifier>urn:oid:BIG</identifier>"
        f"<sender>CWTO</sender><sent>{(now - timedelta(hours=1)).isoformat()}</sent>"
        "<status>Actual</status><msgType>Alert</msgType><scope>Public</scope>"
        "<info><language>en-CA</language><category>Met</category>"
        "<event>Air Quality Warning</event><urgency>Immediate</urgency>"
        "<severity>Moderate</severity><certainty>Likely</certainty>"
        f"<expires>{(now + timedelta(days=1)).isoformat()}</expires>"
        "<headline>Air Quality Warning in effect</headline>"
        f"<description>{description}</description>"
        "<parameter><valueName>layer:EC-MSC-SMC:1.0:Alert_Type</valueName>"
        "<value>warning</value></parameter>"
        "<area><areaDesc>Ottawa</areaDesc>"
        "<geocode><valueName>profile:CAP-CP:Location:0.3</valueName>"
        "<value>3506008</value></geocode>"
        "</area></info>"
        "<info><language>fr-CA</language><category>Met</category>"
        "<event>Avertissement de qualite de l'air</event><urgency>Immediate</urgency>"
        "<severity>Moderate</severity><certainty>Likely</certainty>"
        f"<expires>{(now + timedelta(days=1)).isoformat()}</expires>"
        "<headline>Avertissement en vigueur</headline>"
        f"<description>{description_alt}</description>"
        "<area><areaDesc>Ottawa</areaDesc>"
        "<geocode><valueName>profile:CAP-CP:Location:0.3</valueName>"
        "<value>3506008</value></geocode>"
        "</area></info></alert>"
    )


def _atom(cap_url: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<entry><id>atom-0</id><title>Air Quality Warning</title>"
        '<category term="status=Actual"/>'
        f'<link type="application/cap+xml" href="{cap_url}"/>'
        "</entry></feed>"
    )


@pytest.mark.asyncio
async def test_an_oversized_alert_still_fits_what_the_recorder_stores(
    hass, aioclient_mock, enable_custom_integrations
):
    """A 30 KB bilingual alert, of the shape that overflows in production."""
    cap_url = "https://cap.example/big.cap"
    aioclient_mock.get(FEED, text=_atom(cap_url))
    aioclient_mock.get(cap_url, text=_cap_xml("D" * 15000, "A" * 15000))

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ECCC: Ontario",
        data={"provider": "eccc", "province": "ON"},
        options={"streaming": False, "scan_interval": 300},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    states = [
        state
        for state in hass.states.async_all("sensor")
        if state.entity_id.startswith("sensor.cap_alerts_eccc_cap_alert_")
    ]
    assert len(states) == 1
    attrs = dict(states[0].attributes)

    # Measured the way the recorder measures it: the state as HA finished it,
    # ``friendly_name`` and ``icon`` included, minus the unrecorded set. Those
    # trailing names are exactly what the reserve inside PAYLOAD_BUDGET covers,
    # so the ceiling — not the budget — is the assertion that matters here.
    recorded = {k: v for k, v in attrs.items() if k not in UNRECORDED_ATTRIBUTES}
    assert len(json_bytes(recorded)) < RECORDER_CEILING
    assert measure(attrs) <= PAYLOAD_BUDGET + len(json_bytes(attrs["friendly_name"]))

    # The alternate paid the whole bill, so the language the user asked for
    # came through untouched.
    assert "description_alt" not in attrs
    assert attrs["description"] == "D" * 15000
    # Unrecorded, but still on the state for templates and the card. The
    # declaration is what takes it out of the bound, so pin it where HA reads
    # it rather than on the class attribute.
    assert attrs["parameters"]
    # (The sensor platform contributes its own, so this is a superset.)
    assert UNRECORDED_ATTRIBUTES <= states[0].state_info["unrecorded_attributes"]
