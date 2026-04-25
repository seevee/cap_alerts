"""Constants for the CAP Alerts integration."""

from __future__ import annotations

DOMAIN = "cap_alerts"
PLATFORMS = ["sensor"]

# RFC §2.7 — bumped on breaking attribute/event payload changes
PLATFORM_VERSION = "1.0"

# RFC §2.3 event names — forward-compatible with an upstream `incident.*` domain.
EVENT_INCIDENT_CREATED = "incident_created"
EVENT_INCIDENT_UPDATED = "incident_updated"
EVENT_INCIDENT_REMOVED = "incident_removed"

# User-Agent for API requests — {0} is replaced with the HA instance ID
USER_AGENT = "HomeAssistant-CAPAlerts/{0}"

# Config keys
CONF_PROVIDER = "provider"
CONF_ZONE_ID = "zone_id"
CONF_GPS_LOC = "gps_loc"
CONF_TRACKER_ENTITY = "tracker_entity"
CONF_PROVINCE = "province"
CONF_COUNTRY = "country"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_TIMEOUT = "timeout"
CONF_LANGUAGE = "language"

# Defaults
DEFAULT_SCAN_INTERVAL = 300  # seconds
DEFAULT_TIMEOUT = 30  # seconds

# ECCC valid province codes
ECCC_PROVINCES = {
    "AB",
    "BC",
    "MB",
    "NB",
    "NL",
    "NS",
    "NT",
    "NU",
    "ON",
    "PE",
    "QC",
    "SK",
    "YT",
}

# MeteoAlarm legacy Atom feed slugs, keyed by ISO 3166-1 alpha-2 country
# code. The feed URL is country-name-slugged, not code-slugged — verified
# against https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-<slug>.
METEOALARM_COUNTRY_SLUGS: dict[str, str] = {
    "AT": "austria",
    "BE": "belgium",
    "BA": "bosnia-herzegovina",
    "BG": "bulgaria",
    "HR": "croatia",
    "CY": "cyprus",
    "CZ": "czechia",
    "DK": "denmark",
    "EE": "estonia",
    "FI": "finland",
    "FR": "france",
    "DE": "germany",
    "GR": "greece",
    "HU": "hungary",
    "IS": "iceland",
    "IE": "ireland",
    "IL": "israel",
    "IT": "italy",
    "LV": "latvia",
    "LT": "lithuania",
    "LU": "luxembourg",
    "MT": "malta",
    "MD": "moldova",
    "ME": "montenegro",
    "NL": "netherlands",
    "MK": "republic-of-north-macedonia",
    "NO": "norway",
    "PL": "poland",
    "PT": "portugal",
    "RO": "romania",
    "RS": "serbia",
    "SK": "slovakia",
    "SI": "slovenia",
    "ES": "spain",
    "SE": "sweden",
    "CH": "switzerland",
    "UK": "united-kingdom",
}

METEOALARM_COUNTRIES = frozenset(METEOALARM_COUNTRY_SLUGS)
