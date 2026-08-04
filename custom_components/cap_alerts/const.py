"""Constants for the CAP Alerts integration."""

from __future__ import annotations

DOMAIN = "cap_alerts"
PLATFORMS = ["binary_sensor", "button", "sensor"]

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
CONF_COUNTRY_ENTITY = "country_entity"
CONF_COUNTRY_ATTRIBUTE = "country_attribute"
CONF_REGIONS = "regions"
CONF_REGION_LABELS = "region_labels"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_TIMEOUT = "timeout"
CONF_LANGUAGE = "language"
CONF_EXCLUDE_MARINE = "exclude_marine"
CONF_STREAMING = "streaming"
CONF_FEED_SOURCE = "feed_source"

# Defaults
DEFAULT_SCAN_INTERVAL = 300  # seconds
DEFAULT_TIMEOUT = 30  # seconds

# ECCC GeoRSS feed source (ECCC options flow only). "auto" fetches both NAAD
# hosts and unions their entries deduplicated by CAP OID, because neither host
# alone is complete: rss.alertready.ca persistently omits a handful of live
# Actual alerts that rss.naad-adna.pelmorex.com carries (issue #38), while the
# legacy pelmorex host retains a shorter (~13.5 h) window and drops older alerts
# alertready still serves. The named values ("alertready" / "pelmorex") pin a
# single host as an escape hatch. An absent option means "auto" so existing
# entries get the fix without reconfiguring. The pelmorex host sunsets ~late
# Sept 2026; removing it then is a cleanup task, not an outage (a failing host
# is skipped as long as the other succeeds).
DEFAULT_FEED_SOURCE = "auto"

# ECCC/NAAD real-time streaming feed. The NAADS 2.0 LMD User Guide documents the
# TCP streaming feed — not the auxiliary GeoRSS feed — as the correct channel for
# 24/7 automated systems. streaming.alertready.ca:8443 is the surviving-domain TLS
# endpoint (the deprecated pelmorex streaming1/2:8080 hosts sunset ~Sept 2026 and
# drop unregistered clients). When streaming is enabled the GeoRSS feed is retained
# only as the startup/reconnect backfill source; the coordinator's update_interval
# is repurposed as the periodic safety-resync cadence.
NAAD_STREAM_HOST = "streaming.alertready.ca"
NAAD_STREAM_PORT = 8443
# A NAADS heartbeat is a CAP <alert> whose <sender> starts with this prefix and
# whose <status> is "System"; it is emitted at least every 60 s and carries a
# <references> list of recent alert OIDs. Silence past the watchdog timeout forces
# a reconnect.
NAAD_STREAM_HEARTBEAT_SENDER_PREFIX = "NAADS-Heartbeat"
NAAD_STREAM_HEARTBEAT_TIMEOUT_S = 130
# Exponential reconnect backoff bounds (seconds).
NAAD_STREAM_BACKOFF_MIN_S = 1
NAAD_STREAM_BACKOFF_MAX_S = 60
# Periodic GeoRSS safety-resync interval (seconds) used as the coordinator
# update_interval when streaming, replacing the fast GeoRSS poll.
DEFAULT_STREAM_RESYNC_INTERVAL = 1800
# Floor between GeoRSS backfills triggered by a stream *reconnect*. Backoff only
# grows for connections that delivered nothing, so an endpoint that sends a
# heartbeat and then drops — or goes half-open — reconnects at the heartbeat or
# watchdog cadence with the backoff pinned at its floor, and every reconnect
# costs a full ~7 MB feed fetch. This bounds that to the GeoRSS poll cadence
# streaming replaced, so a flapping socket can never cost more than polling did.
# The periodic resync is never gated by it: that fetch is the availability signal.
NAAD_STREAM_BACKFILL_MIN_INTERVAL_S = DEFAULT_SCAN_INTERVAL

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

# Country values a source entity may report that don't match the tables
# above. MeteoAlarm's own codes diverge from ISO 3166 ("UK" vs "GB"), and
# reverse geocoders report the ISO *official* English name — verified
# 2026-07-02 against BigDataCloud (GeoLocator's backend), which returns e.g.
# "United Kingdom of Great Britain and Northern Ireland (the)" and
# "North Macedonia". Name keys are casefolded and matched after stripping
# parenthetical suffixes.
METEOALARM_COUNTRY_CODE_ALIASES: dict[str, str] = {
    "GB": "UK",  # ISO 3166-1 alpha-2
    "EL": "GR",  # EU institutional code for Greece
}
METEOALARM_COUNTRY_NAME_ALIASES: dict[str, str] = {
    "united kingdom of great britain and northern ireland": "UK",
    "great britain": "UK",
    "north macedonia": "MK",
    "republic of moldova": "MD",
    "czech republic": "CZ",
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

# Primary subtags observed in live SWIC CAP bodies (all 140 registry sources
# swept 2026-08-03). Seeds the options-flow language dropdown, which accepts a
# custom value — this is a convenience list, not an exhaustive or validated
# set, and a tag absent from it (or carrying script/region subtags, e.g.
# "zh-Hans", "pt-BR") is still accepted and matched by the provider.
WMO_LANGUAGES: tuple[str, ...] = (
    "auto",
    "ar",
    "bg",
    "bs",
    "cnr",
    "cs",
    "da",
    "de",
    "el",
    "en",
    "es",
    "et",
    "fi",
    "fr",
    "he",
    "hr",
    "hu",
    "id",
    "is",
    "it",
    "kl",
    "lt",
    "lv",
    "mk",
    "mt",
    "nl",
    "no",
    "pl",
    "pt",
    "rm",
    "ro",
    "ru",
    "sk",
    "sl",
    "sr",
    "sv",
    "th",
    "zh",
)
