"""Event-type → mdi icon dispatch."""

from __future__ import annotations

import pytest

from custom_components.cap_alerts.icons import FALLBACK_ICON, icon_for


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("Tornado Warning", "mdi:weather-tornado"),
        ("Severe Thunderstorm Warning", "mdi:weather-lightning"),
        ("Flash Flood Warning", "mdi:water"),
        ("Winter Storm Warning", "mdi:snowflake-alert"),
        ("Excessive Heat Warning", "mdi:weather-sunny-alert"),
        ("Red Flag Warning", "mdi:fire"),
        ("High Wind Warning", "mdi:weather-windy"),
        ("Dense Fog Advisory", "mdi:weather-fog"),
        ("Air Quality Alert", "mdi:smog"),
        ("Special Weather Statement", "mdi:alert-circle"),
        ("Hurricane Warning", "mdi:weather-hurricane"),
        ("Tsunami Warning", "mdi:tsunami"),
    ],
)
def test_nws_events(alert_factory, event, expected):
    assert icon_for(alert_factory(event=event, provider="nws")) == expected


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("severe thunderstorm warning issued", "mdi:weather-lightning"),
        ("blizzard warning in effect", "mdi:snowflake-alert"),
        ("rainfall warning", "mdi:weather-pouring"),
        ("extreme cold warning", "mdi:snowflake-thermometer"),
        ("fog advisory", "mdi:weather-fog"),
    ],
)
def test_eccc_events(alert_factory, event, expected):
    assert icon_for(alert_factory(event=event, provider="eccc")) == expected


def test_eccc_title_case_event_still_maps_icon(alert_factory):
    """CAP-body title-case shift (e.g. 'Freezing Drizzle Advisory') keeps icon dispatch."""
    assert (
        icon_for(alert_factory(event="Freezing Drizzle Advisory", provider="eccc"))
        == "mdi:snowflake-melt"
    )


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("Wind", "mdi:weather-windy"),
        ("Forest fire", "mdi:fire"),
        ("Avalanches", "mdi:snowflake-alert"),
        ("Snow/Ice", "mdi:snowflake"),
        ("Thunderstorm", "mdi:weather-lightning"),
        ("Fog", "mdi:weather-fog"),
        ("Coastal Event", "mdi:waves"),
        ("Rain-Flood", "mdi:home-flood"),
        ("Extreme high temperature", "mdi:weather-sunny-alert"),
        ("Extreme low temperature", "mdi:snowflake-thermometer"),
        ("EXTREME HIGH TEMP", "mdi:weather-sunny-alert"),
        ("EXTREME LOW TEMP", "mdi:snowflake-thermometer"),
        ("FORESTFIRE", "mdi:fire"),
        ("Wave height warning", "mdi:waves"),
        ("Green high_temperature warning", "mdi:weather-sunny-alert"),
        ("Green low_temperature warning", "mdi:snowflake-thermometer"),
        ("Minor high-temperature warning", "mdi:weather-sunny-alert"),
        ("Minor low-temperature warning", "mdi:snowflake-thermometer"),
    ],
)
def test_meteoalarm_events(alert_factory, event, expected):
    assert icon_for(alert_factory(event=event, provider="meteoalarm")) == expected


@pytest.mark.parametrize(
    ("awareness_type", "expected"),
    [
        ("1; Wind", "mdi:weather-windy"),
        ("2; snow-ice", "mdi:snowflake"),
        ("3; Thunderstorm", "mdi:weather-lightning"),
        ("4; Fog", "mdi:weather-fog"),
        ("5; high-temperature", "mdi:weather-sunny-alert"),
        ("6; low-temperature", "mdi:snowflake-thermometer"),
        ("7; coastalevent", "mdi:waves"),
        ("8; forest-fire", "mdi:fire"),
        ("9; avalanches", "mdi:snowflake-alert"),
        ("10; Rain", "mdi:weather-pouring"),
        ("12; flooding", "mdi:home-flood"),
        ("13; rain-flood", "mdi:home-flood"),
        ("14; Marine-Hazard", "mdi:waves"),
        ("15; Drought", "mdi:water-off"),
        # Members spell the label however they like; only the code is stable.
        ("1; wind", "mdi:weather-windy"),
        ("5; High-temperature", "mdi:weather-sunny-alert"),
        (
            "5; high-temperature may threat public safety and/or damage infrastructure",
            "mdi:weather-sunny-alert",
        ),
    ],
)
def test_meteoalarm_awareness_type_classifies(alert_factory, awareness_type, expected):
    """The EUMETNET code decides, whatever language the event is written in."""
    alert = alert_factory(
        event="Tuulivaroitus maa-alueille",
        language="fi-FI",
        provider="meteoalarm",
        parameters={"awareness_type": awareness_type},
    )
    assert icon_for(alert) == expected


def test_finnish_alert_with_swedish_alternate(alert_factory):
    """Issue #97: FMI publishes fi/sv/en; before #154 the alternate was Swedish.

    The #91 English-alternate path can't reach the English event here, and some
    FMI alerts carry no English block at all — the awareness code does.
    """
    alert = alert_factory(
        event="Metsäpalovaroitus",
        event_alt="Skogsbrandsvarning",
        language="fi-FI",
        language_alt="sv-FI",
        provider="meteoalarm",
        parameters={"awareness_type": "8; forest-fire"},
    )
    assert icon_for(alert) == "mdi:fire"


def test_awareness_type_beats_the_event_tables(alert_factory):
    """A coded hazard wins over matchable event text, in any language.

    MeteoFrance green markers are ``Vent`` bulletins whose event text mentions
    the season, so a text match is the less authoritative signal even when it
    fires.
    """
    alert = alert_factory(
        event="Heat wave",
        provider="meteoalarm",
        parameters={"awareness_type": "1; Wind"},
    )
    assert icon_for(alert) == "mdi:weather-windy"


def test_unknown_awareness_code_falls_through_to_event(alert_factory):
    """A code we don't know (profile revision) still gets the event tables."""
    alert = alert_factory(
        event="Thunderstorm",
        provider="meteoalarm",
        parameters={"awareness_type": "99; something-new"},
    )
    assert icon_for(alert) == "mdi:weather-lightning"


def test_awareness_type_ignored_off_meteoalarm(alert_factory):
    """WMO and ECCC publish no ``awareness_type``; nothing else may read it."""
    alert = alert_factory(
        event="Tornado Warning",
        provider="wmo",
        parameters={"awareness_type": "8; forest-fire"},
    )
    assert icon_for(alert) == "mdi:weather-tornado"


def test_meteoalarm_unknown_event_falls_back(alert_factory):
    assert (
        icon_for(alert_factory(event="Volcanic ash plume", provider="meteoalarm"))
        == FALLBACK_ICON
    )


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("Earthquake", "mdi:pulse"),
        ("Volcano", "mdi:volcano"),
        ("Tropical Cyclone", "mdi:weather-hurricane"),
        ("Flood", "mdi:home-flood"),
        ("Tsunami", "mdi:tsunami"),
        ("Drought", "mdi:water-off"),
        ("Wildfire", "mdi:fire"),
    ],
)
def test_gdacs_events(alert_factory, event, expected):
    """Non-weather hazards get their own icons; no weather table carries them."""
    assert icon_for(alert_factory(event=event, provider="gdacs")) == expected


def test_gdacs_unknown_event_falls_back(alert_factory):
    assert (
        icon_for(alert_factory(event="Geomagnetic Storm", provider="gdacs"))
        == FALLBACK_ICON
    )


def test_gdacs_table_does_not_leak_to_other_providers(alert_factory):
    """``Earthquake`` is a GDACS name; an NWS alert must not borrow its icon."""
    assert icon_for(alert_factory(event="Earthquake", provider="nws")) == FALLBACK_ICON


def test_unknown_event_falls_back(alert_factory):
    assert (
        icon_for(alert_factory(event="Completely Made Up Hazard", provider="nws"))
        == FALLBACK_ICON
    )


def test_empty_event_falls_back(alert_factory):
    assert icon_for(alert_factory(event="", provider="nws")) == FALLBACK_ICON


def test_heat_wave_is_not_a_coastal_alert(alert_factory):
    """'Heat wave' must not match the ``wave`` needle (observed live on MeteoAlarm CH)."""
    assert (
        icon_for(alert_factory(event="Heat wave", provider="meteoalarm"))
        == "mdi:weather-sunny-alert"
    )


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("tsunami", "mdi:tsunami"),
        ("tornado", "mdi:weather-tornado"),
        ("hurricane", "mdi:weather-hurricane"),
        ("smog", "mdi:smog"),
        ("air quality", "mdi:smog"),
    ],
)
def test_meteoalarm_reaches_shared_vocabulary(alert_factory, event, expected):
    """Hazards absent from the EUMETNET table still resolve.

    These fell back before: the MeteoAlarm branch returned early rather than
    consulting the list below it, which carries all five.
    """
    assert icon_for(alert_factory(event=event, provider="meteoalarm")) == expected


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("Freezing Rain Warning", "mdi:snowflake-melt"),
        ("Snow Squall Warning", "mdi:snowflake-alert"),
    ],
)
def test_specific_winter_needles_beat_broad_substrings(alert_factory, event, expected):
    """``freezing rain``/``snow squall`` must not take the broad rain/snow icon.

    WMO consults the EUMETNET table before the ECCC list, so without their own
    needles there the broad ``rain``/``snow`` entries would shadow ECCC's
    specific mappings below.
    """
    assert icon_for(alert_factory(event=event, provider="wmo")) == expected


@pytest.mark.parametrize(
    ("provider", "event", "event_alt", "language", "language_alt", "expected"),
    [
        # The reported case: Chinese WMO body, English alternate block.
        (
            "wmo",
            "高温",
            "high temperature",
            "zh-CN",
            "en-US",
            "mdi:weather-sunny-alert",
        ),
        ("wmo", "雷电", "thunderstorm", "zh-CN", "en-US", "mdi:weather-lightning"),
        ("wmo", "暴雨", "heavy rain", "zh-CN", "en-US", "mdi:weather-pouring"),
        # Same defect, other providers — this is not WMO-specific.
        (
            "meteoalarm",
            "Hitzewelle",
            "Heat wave",
            "de",
            "en",
            "mdi:weather-sunny-alert",
        ),
        (
            "eccc",
            "Avertissement Orange - Qualité De L'Air",
            "Orange Warning - Air Quality",
            "fr-CA",
            "en-CA",
            "mdi:smog",
        ),
    ],
)
def test_localized_event_classifies_on_english_alternate(
    alert_factory, provider, event, event_alt, language, language_alt, expected
):
    """A localized ``<event>`` matches no keyword; the English block is used instead."""
    assert (
        icon_for(
            alert_factory(
                event=event,
                event_alt=event_alt,
                language=language,
                language_alt=language_alt,
                provider=provider,
            )
        )
        == expected
    )


def test_non_english_alternate_does_not_hijack_classification(alert_factory):
    """A document with no English block yields a non-English alternate (#154)."""
    assert (
        icon_for(
            alert_factory(
                event="高温",
                event_alt="temperatura alta",
                language="zh-CN",
                language_alt="pt-PT",
                provider="wmo",
            )
        )
        == FALLBACK_ICON
    )


def test_english_primary_classifies_on_its_own_event(alert_factory):
    """A non-English alternate is ignored when the presented block is English."""
    assert (
        icon_for(
            alert_factory(
                event="Heat wave",
                event_alt="Hitzewelle",
                language="en",
                language_alt="de",
                provider="meteoalarm",
            )
        )
        == "mdi:weather-sunny-alert"
    )


def test_alternate_ignored_without_language_tags(alert_factory):
    """A single-language document classifies on its own event (e.g. WMO th-TH)."""
    assert (
        icon_for(
            alert_factory(event="Very Heavy Rain", language="th-TH", provider="wmo")
        )
        == "mdi:weather-pouring"
    )
