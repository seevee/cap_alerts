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
    ],
)
def test_meteoalarm_events(alert_factory, event, expected):
    assert icon_for(alert_factory(event=event, provider="meteoalarm")) == expected


def test_meteoalarm_unknown_event_falls_back(alert_factory):
    assert (
        icon_for(alert_factory(event="Volcanic ash plume", provider="meteoalarm"))
        == FALLBACK_ICON
    )


def test_unknown_event_falls_back(alert_factory):
    assert (
        icon_for(alert_factory(event="Completely Made Up Hazard", provider="nws"))
        == FALLBACK_ICON
    )


def test_empty_event_falls_back(alert_factory):
    assert icon_for(alert_factory(event="", provider="nws")) == FALLBACK_ICON
