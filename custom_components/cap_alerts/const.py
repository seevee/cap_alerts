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
