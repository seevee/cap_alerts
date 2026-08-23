"""Tests for ``_compute_device_title`` across all provider/filter modes."""

from __future__ import annotations

from custom_components.cap_alerts.flows.common import (
    _compute_device_title as _compute,
)
from custom_components.cap_alerts.const import (
    CONF_COUNTRY,
    CONF_COUNTRY_ENTITY,
    CONF_GPS_LOC,
    CONF_PROVIDER,
    CONF_PROVINCE,
    CONF_REGION_LABELS,
    CONF_REGIONS,
    CONF_SOURCE_ID,
    CONF_TRACKER_ENTITY,
    CONF_ZONE_ID,
)


# --- NWS ---------------------------------------------------------------------


def test_nws_zone_title():
    assert (
        _compute({CONF_PROVIDER: "nws", CONF_ZONE_ID: "ALC001"})
        == "CAP Alerts NWS (ALC001)"
    )


def test_nws_gps_title():
    assert (
        _compute({CONF_PROVIDER: "nws", CONF_GPS_LOC: "40.7,-74.0"})
        == "CAP Alerts NWS (40.7,-74.0)"
    )


def test_nws_tracker_title():
    assert (
        _compute({CONF_PROVIDER: "nws", CONF_TRACKER_ENTITY: "device_tracker.phone"})
        == "CAP Alerts NWS (phone)"
    )


# --- ECCC --------------------------------------------------------------------


def test_eccc_province_title():
    assert (
        _compute({CONF_PROVIDER: "eccc", CONF_PROVINCE: "ON"}) == "CAP Alerts ECCC (ON)"
    )


def test_eccc_gps_title():
    assert (
        _compute({CONF_PROVIDER: "eccc", CONF_GPS_LOC: "45.4,-75.7"})
        == "CAP Alerts ECCC (45.4,-75.7)"
    )


def test_eccc_tracker_title():
    assert (
        _compute({CONF_PROVIDER: "eccc", CONF_TRACKER_ENTITY: "device_tracker.phone"})
        == "CAP Alerts ECCC (phone)"
    )


# --- MeteoAlarm --------------------------------------------------------------


def test_meteoalarm_country_only_uses_friendly_name():
    assert (
        _compute({CONF_PROVIDER: "meteoalarm", CONF_COUNTRY: "DE"})
        == "CAP Alerts METEOALARM (Germany)"
    )


def test_meteoalarm_gps_polygon_uses_lat_lon():
    data = {
        CONF_PROVIDER: "meteoalarm",
        CONF_COUNTRY: "DE",
        CONF_GPS_LOC: "52.52,13.405",
    }
    assert _compute(data) == "CAP Alerts METEOALARM (52.52,13.405)"


def test_meteoalarm_region_picker_single():
    data = {
        CONF_PROVIDER: "meteoalarm",
        CONF_COUNTRY: "DE",
        CONF_REGIONS: ["DE100"],
        CONF_REGION_LABELS: {"DE100": "Erzgebirgskreis"},
    }
    assert _compute(data) == "CAP Alerts METEOALARM (DE — Erzgebirgskreis)"


def test_meteoalarm_region_picker_multi():
    data = {
        CONF_PROVIDER: "meteoalarm",
        CONF_COUNTRY: "DE",
        CONF_REGIONS: ["DE100", "DE200", "DE300"],
        CONF_REGION_LABELS: {
            "DE100": "Saxony",
            "DE200": "Bavaria",
            "DE300": "Hesse",
        },
    }
    # Bavaria sorts alphabetically first; +2 more.
    assert _compute(data) == "CAP Alerts METEOALARM (DE — Bavaria +2)"


def test_meteoalarm_region_picker_counts_regions_not_labels():
    # An entry written before every selection was labeled can carry fewer
    # labels than regions; the "+N" must count the authoritative selection.
    data = {
        CONF_PROVIDER: "meteoalarm",
        CONF_COUNTRY: "DE",
        CONF_REGIONS: ["DE100", "DE200", "DE300"],
        CONF_REGION_LABELS: {"DE100": "Saxony", "DE200": "Bavaria"},
    }
    assert _compute(data) == "CAP Alerts METEOALARM (DE — Bavaria +2)"


def test_meteoalarm_region_picker_legacy_no_labels():
    data = {
        CONF_PROVIDER: "meteoalarm",
        CONF_COUNTRY: "DE",
        CONF_REGIONS: ["DE100", "DE200", "DE300"],
    }
    assert _compute(data) == "CAP Alerts METEOALARM (Germany — 3 regions)"


def test_meteoalarm_tracker_title():
    # Part A tracker mode carries a fixed country + tracker; title is the bare
    # tracker name, matching the GPS-polygon mode's location-only title.
    data = {
        CONF_PROVIDER: "meteoalarm",
        CONF_COUNTRY: "DE",
        CONF_TRACKER_ENTITY: "device_tracker.phone",
    }
    assert _compute(data) == "CAP Alerts METEOALARM (phone)"


def test_meteoalarm_country_source_title():
    # Part B fully-mobile mode has no static country; title reads "auto: <name>".
    data = {
        CONF_PROVIDER: "meteoalarm",
        CONF_TRACKER_ENTITY: "device_tracker.van",
        CONF_COUNTRY_ENTITY: "sensor.geolocator_country",
    }
    assert _compute(data) == "CAP Alerts METEOALARM (auto: van)"


# --- WMO ---------------------------------------------------------------------


def test_wmo_country_wide_uses_source_name():
    assert (
        _compute({CONF_PROVIDER: "wmo", CONF_SOURCE_ID: "mx-smn-es"})
        == "CAP Alerts WMO (Mexico (SMN, Spanish))"
    )


def test_wmo_gps_appends_coordinates_to_source_name():
    data = {
        CONF_PROVIDER: "wmo",
        CONF_SOURCE_ID: "mx-smn-es",
        CONF_GPS_LOC: "19.4326,-99.1332",
    }
    assert _compute(data) == "CAP Alerts WMO (Mexico (SMN, Spanish) (19.4326,-99.1332))"


def test_wmo_unlisted_source_falls_back_to_id():
    assert (
        _compute({CONF_PROVIDER: "wmo", CONF_SOURCE_ID: "xx-foo-en"})
        == "CAP Alerts WMO (xx-foo-en)"
    )


def test_wmo_tracker_title():
    # WMO nests the tracker name inside the source name, mirroring WMO GPS.
    data = {
        CONF_PROVIDER: "wmo",
        CONF_SOURCE_ID: "mx-smn-es",
        CONF_TRACKER_ENTITY: "device_tracker.phone",
    }
    assert _compute(data) == "CAP Alerts WMO (Mexico (SMN, Spanish) (phone))"


# --- GDACS -------------------------------------------------------------------


def test_gdacs_global_title():
    # The feed is worldwide, so a scope-less entry is complete, not "Unknown".
    assert _compute({CONF_PROVIDER: "gdacs"}) == "CAP Alerts GDACS (Global)"


def test_gdacs_gps_title():
    assert (
        _compute({CONF_PROVIDER: "gdacs", CONF_GPS_LOC: "35.6762,139.6503"})
        == "CAP Alerts GDACS (35.6762,139.6503)"
    )


def test_gdacs_tracker_title():
    # Must not read as "Global": the tracker case comes before the GPS default.
    assert (
        _compute({CONF_PROVIDER: "gdacs", CONF_TRACKER_ENTITY: "device_tracker.phone"})
        == "CAP Alerts GDACS (phone)"
    )


# --- Fallback ----------------------------------------------------------------


def test_unrecognized_data_falls_back_to_unknown():
    """No flow produces this today, but a provider added without a title case
    would, and the entry still needs a name it can be renamed from."""
    assert _compute({CONF_PROVIDER: "nws"}) == "CAP Alerts NWS (Unknown)"
