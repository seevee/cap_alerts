"""FMI episode merge: a warning split at the window edge (#98).

FMI's shape differs from MeteoFrance's in two ways these tests pin down. It
packs every warned region into ONE ``<area>`` block (N ``EMMA_ID`` geocodes and
an ``areaDesc`` naming all N), so a per-area-block explode would leave an alert
scoped to the whole set; and it does not publish one bulletin per calendar day,
so a calendar-day collapse would silently drop one of two same-day advisories.

Payloads are built rather than kept as JSON fixtures, matching
``test_meteoalarm_episodes``: the shapes differ from one another by a window or
a region, and a builder keeps that difference legible.

Every test injects ``now`` — the merge drops warnings whose window has closed,
so without injection these would pass or fail depending on the run date.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from custom_components.cap_alerts.providers import meteoalarm


FMI = "cap@fmi.fi"
MF = "vigilance@meteo.fr"
DWD = "dwd@dwd.de"

YELLOW = "2; yellow; Moderate"
WILDFIRE = "8; forest fire"
WIND = "1; wind"

# EMMA_ID → the name FMI publishes for it, in ``areaDesc`` order.
NAMES = {
    "FI020": "Ahvenanmaa",
    "FI032": "Päijät-Häme",
    "FI034": "Kanta-Häme",
    "FI036": "Kymenlaakso",
    "FI043": "Etelä-Karjala",
    "FI809": "Perämeren pohjoisosa",
    "FR101": "Paris",
    "DE100": "Berlin",
}


def _warning(
    *,
    onset: str,
    expires: str,
    regions: tuple[str, ...],
    event: str = "Maastopalovaroitus",
    awareness_type: str = WILDFIRE,
    level: str = YELLOW,
    sender: str = FMI,
    scheme: str = "EMMA_ID",
    split_areas: bool = False,
    sent: str = "",
    uid: str = "",
) -> dict:
    """One warning message.

    Defaults to the FMI shape: a single ``<area>`` block carrying every region
    code and an ``areaDesc`` naming each of them in geocode order.
    ``split_areas`` gives the MeteoFrance shape instead — one block per region,
    each with its own name and code.
    """
    groups = tuple((code,) for code in regions) if split_areas else (regions,)
    sent = sent or f"{onset[:10]}T05:05:23+03:00"
    uid = uid or f"{onset[:16]}-{regions[0]}"
    return {
        "uuid": f"uuid-{uid}",
        "alert": {
            "identifier": f"2.49.0.0.246.0.FI.{uid}",
            "sender": sender,
            "sent": sent,
            "status": "Actual",
            "msgType": "Alert",
            "scope": "Public",
            "info": [
                {
                    "language": "fi-FI",
                    "category": ["Met"],
                    "event": event,
                    "severity": "Moderate",
                    "urgency": "Future",
                    "certainty": "Likely",
                    "onset": onset,
                    "expires": expires,
                    "headline": f"{event} {onset[:10]}",
                    "description": f"Varoitus {onset[:10]}.",
                    "parameter": [
                        {"valueName": "awareness_level", "value": level},
                        {"valueName": "awareness_type", "value": awareness_type},
                    ],
                    "area": [
                        {
                            "areaDesc": ", ".join(NAMES[c] for c in group),
                            "geocode": [
                                {"valueName": scheme, "value": c} for c in group
                            ],
                        }
                        for group in groups
                    ],
                }
            ],
        },
    }


class _Resp:
    def __init__(self, body: str):
        self._body = body
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return json.loads(self._body)


class _Session:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload)

    def get(self, url: str):
        return _Resp(self._body)


async def _fetch(*warnings, regions=None, now):
    """Run the real provider over a built payload."""
    config = {"country": "FI"}
    if regions is not None:
        config["regions"] = regions
    return await meteoalarm.MeteoAlarmProvider().async_fetch(
        _Session({"warnings": list(warnings)}),
        config=config,
        options={"language": "fi"},
        now=now,
    )


# --- the reported case: a warning split at midnight -----------------------

# 22:00 Finnish time on 08-04: the evening message is in effect and the one
# taking over at midnight is already published.
MIDNIGHT_NOW = datetime(2026, 8, 4, 19, tzinfo=timezone.utc)


def _midnight_pair() -> tuple[dict, dict]:
    """The reporter's shape: a window ending exactly where the next begins.

    Taken from the sampled wildfire chain, where the footprint also grows from
    one message to the next — so the region that appears in both must merge
    while the one that only appears in the first must not be widened.
    """
    return (
        _warning(
            onset="2026-08-04T20:29:00+03:00",
            expires="2026-08-05T00:00:00+03:00",
            regions=("FI043", "FI036"),
        ),
        _warning(
            onset="2026-08-05T00:00:00+03:00",
            expires="2026-08-06T00:00:00+03:00",
            regions=("FI036",),
        ),
    )


async def test_midnight_split_merges_per_region():
    first, second = _midnight_pair()
    alerts = await _fetch(first, second, regions=["FI036", "FI043"], now=MIDNIGHT_NOW)

    by_region = {a.area_desc: a for a in alerts}
    assert set(by_region) == {"Kymenlaakso", "Etelä-Karjala"}

    # The region carried by both messages is ONE entity spanning both windows.
    spanning = by_region["Kymenlaakso"]
    assert spanning.onset == "2026-08-04T20:29:00+03:00"
    assert spanning.expires == "2026-08-06T00:00:00+03:00"
    assert [d["date"] for d in spanning.episode_days] == ["2026-08-04", "2026-08-05"]

    # The region only the first message covers keeps its own window.
    ending = by_region["Etelä-Karjala"]
    assert ending.expires == "2026-08-05T00:00:00+03:00"
    assert ending.episode_days == ()


async def test_midnight_split_id_survives_the_handover():
    # The defect #98 reports: the id rolled over when the first message expired,
    # so the entity a dashboard referenced was replaced by a new one. The merged
    # id carries no window component, so the message still live after midnight
    # mints the id the pair had before it.
    first, second = _midnight_pair()
    both = await _fetch(first, second, regions=["FI036"], now=MIDNIGHT_NOW)
    after = await _fetch(
        first,
        second,
        regions=["FI036"],
        now=datetime(2026, 8, 5, 6, tzinfo=timezone.utc),
    )
    assert [a.id for a in both] == [a.id for a in after]

    from custom_components.cap_alerts.conventions import episode_id

    assert both[0].id == episode_id(FMI, "8", ["FI036"], "", fallback="unused")


# --- what a calendar-day rule would get wrong -----------------------------


async def test_two_same_day_advisories_stay_two_entities():
    # Sampled live: two FI809 wind advisories an hour apart on one day, both
    # live. A calendar-day collapse keeps one and drops the other — and its
    # (severity, sent) tie-break cannot even choose, because FMI stamps a whole
    # batch with a single ``sent``, which is why both carry one here.
    batch_sent = "2026-08-06T05:05:23+03:00"
    alerts = await _fetch(
        _warning(
            onset="2026-08-06T09:00:00+03:00",
            expires="2026-08-06T21:00:00+03:00",
            regions=("FI809",),
            event="Kova tuuli merialueella",
            awareness_type=WIND,
            sent=batch_sent,
            uid="wind-day",
        ),
        _warning(
            onset="2026-08-06T22:00:00+03:00",
            expires="2026-08-07T00:00:00+03:00",
            regions=("FI809",),
            event="Kova tuuli merialueella",
            awareness_type=WIND,
            sent=batch_sent,
            uid="wind-night",
        ),
        regions=["FI809"],
        now=datetime(2026, 8, 6, 9, tzinfo=timezone.utc),
    )
    assert len(alerts) == 2
    assert [(a.onset, a.expires) for a in alerts] == [
        ("2026-08-06T09:00:00+03:00", "2026-08-06T21:00:00+03:00"),
        ("2026-08-06T22:00:00+03:00", "2026-08-07T00:00:00+03:00"),
    ]
    # Distinct ids, or the alert store would keep only one of them.
    assert len({a.id for a in alerts}) == 2
    assert all(a.episode_days == () for a in alerts)


async def test_three_same_day_runs_mint_three_distinct_ids():
    # Three disjoint windows on one day for one region and phenomenon. The
    # first run takes the window-free id; the later two re-add their opening
    # window at the dialect's own granularity. A day-truncated tie-break — the
    # MeteoFrance rule — would give the second and third the same id, and the
    # alert store, keying by id, would silently drop one live advisory.
    batch_sent = "2026-08-06T05:05:23+03:00"
    windows = [
        ("2026-08-06T08:00:00+03:00", "2026-08-06T10:00:00+03:00"),
        ("2026-08-06T12:00:00+03:00", "2026-08-06T14:00:00+03:00"),
        ("2026-08-06T16:00:00+03:00", "2026-08-06T18:00:00+03:00"),
    ]
    alerts = await _fetch(
        *(
            _warning(
                onset=onset,
                expires=expires,
                regions=("FI809",),
                event="Kova tuuli merialueella",
                awareness_type=WIND,
                sent=batch_sent,
                uid=f"wind-{i}",
            )
            for i, (onset, expires) in enumerate(windows)
        ),
        regions=["FI809"],
        now=datetime(2026, 8, 6, 4, tzinfo=timezone.utc),
    )
    assert [(a.onset, a.expires) for a in alerts] == windows
    assert len({a.id for a in alerts}) == 3


async def test_a_real_window_gap_stays_two_entities():
    # The one lapse in the sampled wildfire chain: a message ending at midnight
    # and the next starting five hours later. Contiguity is the rule, so this
    # does not merge even though the two are a day apart.
    alerts = await _fetch(
        _warning(
            onset="2026-08-05T20:31:00+03:00",
            expires="2026-08-06T00:00:00+03:00",
            regions=("FI036",),
        ),
        _warning(
            onset="2026-08-06T05:05:00+03:00",
            expires="2026-08-07T03:00:00+03:00",
            regions=("FI036",),
        ),
        regions=["FI036"],
        now=datetime(2026, 8, 5, 19, tzinfo=timezone.utc),
    )
    assert len(alerts) == 2
    assert len({a.id for a in alerts}) == 2


# --- the explode, on FMI's one-block-many-codes shape ---------------------


async def test_explode_scopes_a_single_block_to_one_region():
    # FMI packs every region into one <area>, so a per-block explode would
    # leave this alert named "Ahvenanmaa, Kymenlaakso" and scoped to both codes.
    alerts = await _fetch(
        _warning(
            onset="2026-08-04T20:29:00+03:00",
            expires="2026-08-05T00:00:00+03:00",
            regions=("FI020", "FI036"),
        ),
        regions=["FI036"],
        now=MIDNIGHT_NOW,
    )
    (only,) = alerts
    assert only.area_desc == "Kymenlaakso"
    assert only.geocodes["EMMA_ID"] == ("FI036",)


async def test_explode_names_match_the_picker_labels():
    # The names an exploded entity can carry are exactly the picker's labels,
    # because both come from the same resolver — that equivalence is what the
    # regions_for seam buys, so it is worth asserting rather than assuming.
    warning = _warning(
        onset="2026-08-04T20:29:00+03:00",
        expires="2026-08-05T00:00:00+03:00",
        regions=("FI020", "FI036", "FI043"),
    )
    info = warning["alert"]["info"][0]
    labels = dict(meteoalarm._region_pairs(info))
    alerts = await _fetch(
        warning, regions=["FI020", "FI036", "FI043"], now=MIDNIGHT_NOW
    )
    assert {a.area_desc for a in alerts} == set(labels.values())


# --- country-wide mode (documented limitation) ----------------------------


async def test_country_wide_keeps_the_whole_set_and_still_splits():
    # No configured scope to explode against, so each message keeps its full
    # region set and the key is the set. A stable footprint still merges; a
    # footprint that grows between messages splits the episode. Known
    # limitation, kept because exploding every region here would turn a country
    # feed into one entity per region.
    stable = await _fetch(
        _warning(
            onset="2026-08-04T20:29:00+03:00",
            expires="2026-08-05T00:00:00+03:00",
            regions=("FI043", "FI036"),
        ),
        _warning(
            onset="2026-08-05T00:00:00+03:00",
            expires="2026-08-06T00:00:00+03:00",
            regions=("FI043", "FI036"),
            uid="stable-second",
        ),
        now=MIDNIGHT_NOW,
    )
    assert len(stable) == 1
    assert stable[0].area_desc == "Etelä-Karjala, Kymenlaakso"
    assert stable[0].expires == "2026-08-06T00:00:00+03:00"

    churned = await _fetch(*_midnight_pair(), now=MIDNIGHT_NOW)
    assert len(churned) == 2


# --- other senders in the same batch --------------------------------------


async def test_other_senders_are_untouched_by_the_fmi_dialect():
    # Both dialects declare stages, so each runs over the whole batch and has
    # to leave the other's alerts — and an unregistered sender's — alone.
    french = [
        _warning(
            onset=f"2026-08-0{day}T00:00:00+02:00",
            expires=f"2026-08-0{day + 1}T00:00:00+02:00",
            regions=("FR101",),
            sender=MF,
            scheme="NUTS3",
            split_areas=True,
            event="Vigilance jaune canicule",
            awareness_type="5; Extreme high temperature",
            uid=f"fr-{day}",
        )
        for day in (4, 5)
    ]
    german = [
        _warning(
            onset=f"2026-08-0{day}T00:00:00+02:00",
            expires=f"2026-08-0{day + 1}T00:00:00+02:00",
            regions=("DE100",),
            sender=DWD,
            event="STURMBOEEN",
            uid=f"de-{day}",
        )
        for day in (4, 5)
    ]
    alerts = await _fetch(*_midnight_pair(), *french, *german, now=MIDNIGHT_NOW)

    by_sender: dict[str, list] = {}
    for alert in alerts:
        by_sender.setdefault(alert.sender, []).append(alert)

    # MeteoFrance still merges on its own rule: two forecast days, one entity.
    (episode,) = by_sender[MF]
    assert [d["date"] for d in episode.episode_days] == ["2026-08-04", "2026-08-05"]

    # The unregistered sender declares nothing, so both messages survive with
    # their per-message identifier hash for an id.
    assert len(by_sender[DWD]) == 2
    for alert in by_sender[DWD]:
        assert alert.episode_days == ()
        assert alert.id == hashlib.sha256(alert.identifier.encode()).hexdigest()[:12]

    # And FMI's own pair merged, country-wide, into one per footprint.
    assert len(by_sender[FMI]) == 2
