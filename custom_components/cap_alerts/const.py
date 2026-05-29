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
CONF_SOURCE_ID = "source_id"
CONF_ZONE_ID = "zone_id"
CONF_GPS_LOC = "gps_loc"
CONF_TRACKER_ENTITY = "tracker_entity"
CONF_PROVINCE = "province"
CONF_COUNTRY = "country"
CONF_REGIONS = "regions"
CONF_REGION_LABELS = "region_labels"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_TIMEOUT = "timeout"
CONF_LANGUAGE = "language"

# Defaults
DEFAULT_SCAN_INTERVAL = 300  # seconds
DEFAULT_TIMEOUT = 30  # seconds

# Buddhist-Era calendar correction. Some feeds (TMD, surfaced via WMO SWIC)
# emit Buddhist-Era years — Gregorian + 543 — in CAP dateTime fields, e.g.
# "2568-08-05T22:50:00+07:00". A year at or above this threshold is
# unambiguously BE: no Gregorian weather alert is ~375 years out, while every
# BE year is 2543+, so the offset can be subtracted with no risk to a valid
# timestamp. Applied both to CAP-body ISO strings (normalize) and to the WMO
# RSS envelope's RFC-2822 cap:expires (wmo provider).
MIN_BUDDHIST_ERA_YEAR = 2400
BUDDHIST_ERA_OFFSET = 543

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
    "UA": "ukraine",
    "UK": "united-kingdom",
}

METEOALARM_COUNTRIES = frozenset(METEOALARM_COUNTRY_SLUGS)

# Display labels for the country dropdown. Slugs like
# ``bosnia-herzegovina`` and ``republic-of-north-macedonia`` don't
# title-case correctly, so an explicit mapping is used.
METEOALARM_COUNTRY_NAMES: dict[str, str] = {
    "AT": "Austria",
    "BE": "Belgium",
    "BA": "Bosnia and Herzegovina",
    "BG": "Bulgaria",
    "HR": "Croatia",
    "CY": "Cyprus",
    "CZ": "Czechia",
    "DK": "Denmark",
    "EE": "Estonia",
    "FI": "Finland",
    "FR": "France",
    "DE": "Germany",
    "GR": "Greece",
    "HU": "Hungary",
    "IS": "Iceland",
    "IE": "Ireland",
    "IL": "Israel",
    "IT": "Italy",
    "LV": "Latvia",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "MT": "Malta",
    "MD": "Moldova",
    "ME": "Montenegro",
    "NL": "Netherlands",
    "MK": "Republic of North Macedonia",
    "NO": "Norway",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "RS": "Serbia",
    "SK": "Slovakia",
    "SI": "Slovenia",
    "ES": "Spain",
    "SE": "Sweden",
    "CH": "Switzerland",
    "UA": "Ukraine",
    "UK": "United Kingdom",
}

# WMO SWIC source registry. Fetched at config-flow time to populate the
# source dropdown with every mirror-reachable source. The HTML index 403s,
# but the JSON endpoint serves with the integration's own User-Agent.
WMO_SOURCES_URL = "https://severeweather.wmo.int/v2/json/sources.json"

# WMO-category SWIC sources that are registered but NOT reachable on the
# severeweather.wmo.int mirror (their feeds live only on national domains in
# non-uniform formats). Excluded from the dynamic dropdown so users aren't
# offered sources that 404. Curated from verification on 2026-05-24; a newly
# mirrored source merely stays hidden until this set is updated, and the
# config flow's custom-value entry lets users enter any ID regardless.
WMO_UNMIRRORED_SOURCES: frozenset[str] = frozenset(
    {
        "bf-meteo-en",
        "bi-meteo-en",
        "bj-meteo-en",
        "cd-mettelsat-en",
        "co-ungrd-es",
        "dj-meteo-en",
        "gn-dnm-en",
        "gw-inm-en",
        "ml-meteo-en",
        "mm-dmh-en",
        "mr-onm-en",
        "mz-inam-en",
        "ne-meteo-en",
        "pg-ms-en",
        "st-meteo-en",
        "td-anam-en",
        "tg-dgmn-en",
        "to-tms-en",
        "vu-vms-xx",
        "ws-smd-en",
        "ye-yms-en",
    }
)

# WMO Severe Weather Information Centre (SWIC) source IDs, keyed
# {country}-{agency}-{lang}. The per-source RSS feed lives at
# https://severeweather.wmo.int/v2/cap-alerts/{source-id}/rss.xml.
# Offline fallback for the config-flow dropdown when the live registry
# (WMO_SOURCES_URL) is unreachable; the flow accepts a custom value, so any
# valid SWIC source ID still works. Every entry below was verified reachable
# on the live SWIC mirror on 2026-05-24 (cross-checked against the registry);
# sg-mss-en, tl-dnmg-en, and the SE-Asia alternate-language feeds
# (id-inatews-en, th-tmd-en, tl-dnmg-pt, tl-dnmg-tet) were added and
# verified 2026-05-28.
WMO_SOURCE_NAMES: dict[str, str] = {
    # Americas
    "mx-smn-es": "Mexico (SMN, Spanish)",
    "br-inmet-pt": "Brazil (INMET, Portuguese)",
    "ar-smn-es": "Argentina (SMN, Spanish)",
    "cl-meteo-es": "Chile (DMC, Spanish)",
    # Asia / Pacific
    "in-imd-en": "India (IMD, English)",
    "cn-cma-xx": "China (CMA)",
    "id-inatews-id": "Indonesia (InaTEWS, Indonesian)",
    "id-inatews-en": "Indonesia (InaTEWS, English)",
    "ph-pagasa-en": "Philippines (PAGASA, English)",
    "sg-mss-en": "Singapore (MSS, English)",
    "th-tmd-th": "Thailand (TMD, Thai)",
    "th-tmd-en": "Thailand (TMD, English)",
    "tl-dnmg-en": "Timor-Leste (DNMG, English)",
    "tl-dnmg-pt": "Timor-Leste (DNMG, Portuguese)",
    "tl-dnmg-tet": "Timor-Leste (DNMG, Tetum)",
    "au-bom-en": "Australia (BoM, English)",
    "nz-nms-en": "New Zealand (MetService, English)",
    # Middle East / Africa
    "sa-ncm-ar": "Saudi Arabia (NCM, Arabic)",
    "eg-ema-en": "Egypt (EMA, English)",
    "za-saws-en": "South Africa (SAWS, English)",
    "ke-kmd-en": "Kenya (KMD, English)",
    "ng-nimet-en": "Nigeria (NIMET, English)",
    "gh-gmet-en": "Ghana (GMet, English)",
    "sn-anacim-fr": "Senegal (ANACIM, French)",
    "tz-tma-en": "Tanzania (TMA, English)",
}
