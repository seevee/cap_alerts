"""Bounding the attribute payload the recorder stores (issue #150).

The old per-field soft cap is gone, so these tests pin the two halves of what
replaced it: the byte-accurate truncation primitive, and the ladder that decides
which field pays when a serialized alert doesn't fit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.helpers.json import json_bytes
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
    attrs = {
        "id": "x",
        "parameters": {"blob": "P" * 5000},
        "geocodes": {"SGC": ["4700001"] * 800},
        "attribution": "A" * 500,
    }
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


def test_unrecorded_geocodes_do_not_count_toward_the_budget():
    # 847 SGC codes, the #245 advisory's container: complete on the state,
    # invisible to the recorder, so nothing is trimmed on their account.
    attrs = {
        "id": "x",
        "description": "D" * 500,
        "geocodes": {"SGC": [f"47{i:05d}" for i in range(847)]},
    }
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


def test_the_alternates_pay_before_the_area_list():
    attrs = {
        "id": "x",
        "description_alt": "A" * 1000,
        "area_desc": ", ".join(f"Area {i}" for i in range(200)),
        "description": "D" * 1000,
    }
    # Room for everything but a stub of the alternate: it goes whole, and the
    # list after it is never touched.
    budget = measure({k: v for k, v in attrs.items() if k != "description_alt"}) + 50
    out = fit_to_budget(attrs, budget=budget)

    assert "description_alt" not in out
    assert out["area_desc"] == attrs["area_desc"]
    assert out["description"] == "D" * 1000
    assert measure(out) <= budget


def test_the_area_list_pays_before_the_primary_text():
    # The #245 shape in miniature: the place names outweigh the text, so the
    # list is cut short and the description the user reads is untouched.
    attrs = {
        "id": "x",
        "area_desc": ", ".join(f"Area {i}" for i in range(400)),
        "description": "D" * 1000,
        "instruction": "I" * 200,
    }
    out = fit_to_budget(attrs, budget=2600)

    assert out["area_desc"].endswith("…")
    assert attrs["area_desc"].startswith(out["area_desc"][:-1])
    assert out["description"] == "D" * 1000
    assert out["instruction"] == "I" * 200
    assert measure(out) <= 2600


def test_a_cut_area_list_ends_on_a_whole_name():
    # Byte truncation lands mid-name; the list is backed off to the last
    # separator so every name it still carries is one the feed sent.
    names = [f"Municipality of Somewhere Number {i:03d}" for i in range(400)]
    attrs = {"id": "x", "area_desc": ", ".join(names), "description": "D" * 100}
    out = fit_to_budget(attrs, budget=3000)

    assert out["area_desc"].endswith("…")
    kept = out["area_desc"][:-1].split(", ")
    assert kept == names[: len(kept)]
    assert len(kept) < len(names)
    assert measure(out) <= 3000


def test_a_single_long_area_name_is_still_cut_as_text():
    # Nothing to back off to: one name with no separator keeps the plain
    # character-boundary cut rather than vanishing.
    attrs = {"id": "x", "area_desc": "A" * 3000, "description": "D" * 100}
    out = fit_to_budget(attrs, budget=1000)

    assert out["area_desc"].endswith("…")
    assert len(out["area_desc"]) > 500
    assert measure(out) <= 1000


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


def test_a_payload_that_cannot_be_made_to_fit_keeps_what_it_can(caplog):
    # Nothing on the ladder can rescue an alert whose bulk is a reference list
    # this size, and the ladder is text only (#292). The expendable text still
    # goes; the rest is reported, once, where someone will see it.
    attrs = {
        "id": "unfixable",
        "description": "D" * 1000,
        "references": [f"urn:oid:2.49.0.1.124.{i:04d}" for i in range(400)],
    }
    with caplog.at_level("DEBUG", logger="custom_components.cap_alerts.payload"):
        out = fit_to_budget(attrs, budget=800)
        again = fit_to_budget(attrs, budget=800)

    assert "description" not in out
    assert out["references"] == attrs["references"]
    assert measure(out) > 800
    assert again == out

    reports = [r for r in caplog.records if "still exceeds" in r.message]
    assert [r.levelname for r in reports] == ["WARNING", "DEBUG"]
    assert "unfixable" in reports[0].message


def test_the_245_advisory_fits_without_losing_its_text():
    # The shape measured on the archived document: 291 areas, 291 CLC and 847
    # SGC codes, under 2 KB of text in both languages. Before the fix every
    # text field was deleted and the payload was still 10 KB over.
    names = [
        f"m.r. de Municipalité {i:03d} incluant Village {i:03d}" for i in range(291)
    ]
    attrs = {
        "id": "66bb7b4c0a00",
        "event": "Avis Jaune - Gel",
        "headline": "Avis Jaune - Gel en vigueur",
        "description": "D" * 944,
        "instruction": "I" * 67,
        "description_alt": "A" * 771,
        "instruction_alt": "B" * 55,
        "area_desc": ", ".join(names),
        "geocodes": {
            "CLC": [f"46{i:05d}" for i in range(291)],
            "SGC": [f"47{i:05d}" for i in range(847)],
        },
        "bbox": [-110.0, 49.0, -101.4, 60.0],
    }
    assert len(json_bytes(attrs["area_desc"])) > 13000
    assert len(json_bytes(attrs["geocodes"])) > 11000

    out = fit_to_budget(attrs)

    assert measure(out) <= PAYLOAD_BUDGET
    assert out["description"] == "D" * 944
    assert out["instruction"] == "I" * 67
    assert out["geocodes"] == attrs["geocodes"]
    # Once the codes stop counting the bill is a few hundred bytes. The
    # alternates pay it first, and the list keeps all but its tail at worst:
    # on the archived document that was both English fields and the last
    # handful of the 291 names.
    assert out["area_desc"].startswith(", ".join(names[:280]))


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
    # ``friendly_name``, ``icon`` and ``device_class`` included, minus the
    # unrecorded set. Those trailing names are exactly what the reserve inside
    # PAYLOAD_BUDGET covers, so the ceiling — not the budget — is the assertion
    # that matters here.
    recorded = {k: v for k, v in attrs.items() if k not in UNRECORDED_ATTRIBUTES}
    assert len(json_bytes(recorded)) < RECORDER_CEILING
    appended = {k: attrs[k] for k in ("friendly_name", "device_class")}
    assert measure(attrs) <= PAYLOAD_BUDGET + len(json_bytes(appended))

    # The alternate paid the whole bill, so the language the user asked for
    # came through untouched.
    assert "description_alt" not in attrs
    assert attrs["description"] == "D" * 15000
    # Unrecorded, but still on the state for templates and the card. The
    # declaration is what takes it out of the bound, so pin it where HA reads
    # it rather than on the class attribute.
    assert attrs["parameters"]
    assert attrs["geocodes"] == {"SGC": ["3506008"]}
    # (The sensor platform contributes its own, so this is a superset.)
    assert UNRECORDED_ATTRIBUTES <= states[0].state_info["unrecorded_attributes"]
