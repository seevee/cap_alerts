"""MeteoFrance episode merge: forecast days collapsed into one entity (#37).

Payloads are built rather than kept as JSON fixtures: the shapes under test are
parametric (forecast day × severity × department set) and differ from one
another by a field or two, so a builder keeps the *difference* legible where
five near-identical fixture files would bury it.

Every test injects ``now``. The merge drops forecast days whose window has
closed, so without injection these would pass or fail depending on the date the
suite happens to run.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from custom_components.cap_alerts.providers import meteoalarm


MF = "vigilance@meteo.fr"
PARIS = (("FR101", "Paris"),)
YELLOW = "2; yellow; Moderate"
ORANGE = "3; orange; Severe"
GREEN = "1; green; Minor"
HEAT = "5; Extreme high temperature"
STORM = "3; Thunderstorm"

# Mid-afternoon on 2026-08-03: the 08-03 window is in effect and the 08-04 one
# is already published, which live sampling shows is the normal steady state.
NOW = datetime(2026, 8, 3, 16, 30, tzinfo=timezone.utc)


def _bulletin(
    *,
    day: str,
    level: str = YELLOW,
    awareness_type: str = HEAT,
    departments: tuple[tuple[str, str], ...] = PARIS,
    event: str = "Vigilance jaune canicule",
    sender: str = MF,
    onset: str | None = None,
    expires: str | None = None,
    sent: str | None = None,
    uid: str = "",
) -> dict:
    """One MeteoFrance day-bulletin, defaulting to a 00:00 → 00:00 window."""
    following = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    onset = onset or f"{day}T00:00:00+02:00"
    expires = expires or f"{following}T00:00:00+02:00"
    sent = sent or f"{day}T06:00:00+02:00"
    uid = uid or f"{day}-{awareness_type[0]}-{level[0]}-{departments[0][0]}"
    return {
        "uuid": f"uuid-{uid}",
        "alert": {
            "identifier": f"2.49.0.0.250.0.FR.{uid}",
            "sender": sender,
            "sent": sent,
            "status": "Actual",
            "msgType": "Alert",
            "scope": "Public",
            "info": [
                {
                    "language": "fr-FR",
                    "category": ["Met"],
                    "event": event,
                    "severity": "Moderate",
                    "urgency": "Future",
                    "certainty": "Likely",
                    "onset": onset,
                    "expires": expires,
                    "headline": f"{event} ({day})",
                    "description": f"Bulletin du {day}.",
                    "parameter": [
                        {"valueName": "awareness_level", "value": level},
                        {"valueName": "awareness_type", "value": awareness_type},
                    ],
                    "area": [
                        {
                            "areaDesc": label,
                            "geocode": [{"valueName": "NUTS3", "value": code}],
                        }
                        for code, label in departments
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


async def _fetch(*bulletins, regions=None, now=NOW):
    """Run the real provider over a built payload."""
    config = {"country": "FR"}
    if regions is not None:
        config["regions"] = regions
    return await meteoalarm.MeteoAlarmProvider().async_fetch(
        _Session({"warnings": list(bulletins)}),
        config=config,
        options={"language": "fr"},
        now=now,
    )


# --- merging consecutive forecast days ------------------------------------


async def test_episode_contiguous_days_merge_to_one():
    alerts = await _fetch(
        _bulletin(day="2026-08-03", onset="2026-08-03T16:00:00+02:00"),
        _bulletin(day="2026-08-04"),
        regions=["FR101"],
    )
    assert len(alerts) == 1
    (episode,) = alerts
    assert episode.onset == "2026-08-03T16:00:00+02:00"
    assert episode.expires == "2026-08-05T00:00:00+02:00"


async def test_episode_merged_id_is_window_free():
    # The id must not move when the episode's first day rolls off the feed —
    # that roll-over at midnight is the defect #37 reports.
    both = await _fetch(
        _bulletin(day="2026-08-03"),
        _bulletin(day="2026-08-04"),
        regions=["FR101"],
    )
    second_only = await _fetch(
        _bulletin(day="2026-08-04"),
        regions=["FR101"],
        now=datetime(2026, 8, 4, 10, tzinfo=timezone.utc),
    )
    assert both[0].id == second_only[0].id

    # The content key lives with the rest of the MeteoFrance dialect in the
    # convention table (issue #88); the provider only reaches it via the table.
    from custom_components.cap_alerts.conventions import episode_id

    expected = episode_id(MF, "5", ["FR101"], "", fallback="x")
    assert both[0].id == expected


async def test_episode_gap_day_stays_separate():
    alerts = await _fetch(
        _bulletin(day="2026-08-03"),
        _bulletin(day="2026-08-05"),
        regions=["FR101"],
    )
    assert len(alerts) == 2
    assert alerts[0].id != alerts[1].id


async def test_episode_escalation_takes_max_severity():
    # Middle day escalates. The entity should read orange and stay one entity.
    alerts = await _fetch(
        _bulletin(day="2026-08-03"),
        _bulletin(day="2026-08-04", level=ORANGE, event="Vigilance orange canicule"),
        _bulletin(day="2026-08-05"),
        regions=["FR101"],
    )
    assert len(alerts) == 1
    (episode,) = alerts
    assert episode.parameters["awareness_level"] == ORANGE
    assert episode.event == "Vigilance orange canicule"
    assert episode.onset == "2026-08-03T00:00:00+02:00"
    assert episode.expires == "2026-08-06T00:00:00+02:00"


async def test_episode_days_profile_shape():
    alerts = await _fetch(
        _bulletin(day="2026-08-03"),
        _bulletin(day="2026-08-04", level=ORANGE),
        regions=["FR101"],
    )
    (episode,) = alerts
    assert [d["date"] for d in episode.episode_days] == ["2026-08-03", "2026-08-04"]
    assert [d["severity"] for d in episode.episode_days] == ["moderate", "severe"]
    assert set(episode.episode_days[0]) == {
        "date",
        "onset",
        "expires",
        "severity",
        "awareness_level",
        "event",
        "headline",
        "area_desc",
    }
    # Serialized for the card by the existing tuple branch, no serializer change.
    assert episode.to_attributes()["episode_days"] == list(episode.episode_days)


async def test_episode_days_absent_for_non_merged_provider():
    from custom_components.cap_alerts.model import CAPAlert

    assert CAPAlert(id="x").episode_days == ()
    assert "episode_days" not in CAPAlert(id="x").to_attributes()


# --- region scoping (the N2 finding) --------------------------------------


async def test_episode_stable_across_footprint_churn():
    # Measured on the live feed: a thunderstorm bulletin covered 83 departments
    # one day and 54 the next. Keying an episode on the department *set* — or
    # on its intersection with a multi-department config — splits it in two.
    # Per-department scoping is what makes this hold.
    day1 = (("FR211", "Ardennes"), ("FR212", "Aube"))
    day2 = (("FR211", "Ardennes"), ("FR212", "Aube"), ("FR712", "Cantal"))
    alerts = await _fetch(
        _bulletin(day="2026-08-03", awareness_type=STORM, departments=day1),
        _bulletin(day="2026-08-04", awareness_type=STORM, departments=day2),
        regions=["FR211", "FR212", "FR712"],
    )
    by_region = {a.area_desc: a for a in alerts}
    assert set(by_region) == {"Ardennes", "Aube", "Cantal"}
    # The two departments present on both days each hold ONE episode spanning
    # both windows; the one that joins on day 2 holds its own single-day entity.
    for name in ("Ardennes", "Aube"):
        assert by_region[name].onset == "2026-08-03T00:00:00+02:00"
        assert by_region[name].expires == "2026-08-05T00:00:00+02:00"
        assert len(by_region[name].episode_days) == 2
    assert by_region["Cantal"].onset == "2026-08-04T00:00:00+02:00"
    assert by_region["Cantal"].episode_days == ()  # single day, nothing to profile
    assert len({a.id for a in alerts}) == 3


async def test_explode_scopes_area_desc_to_one_department():
    # Without the explode every entity's area_desc is the bulletin's whole
    # department list — up to 83 names on a live feed.
    wide = tuple((f"FR2{i:02d}", f"Dept{i}") for i in range(1, 17))
    alerts = await _fetch(
        _bulletin(day="2026-08-03", departments=wide),
        regions=["FR203"],
    )
    (only,) = alerts
    assert only.area_desc == "Dept3"
    assert only.geocodes["NUTS3"] == ("FR203",)


async def test_country_wide_uses_full_region_set():
    # No configured scope to explode against, so the bulletin stays whole and
    # the key uses its full department set. Known limitation: a footprint that
    # moves overnight splits the episode here.
    day1 = (("FR211", "Ardennes"), ("FR212", "Aube"))
    stable = await _fetch(
        _bulletin(day="2026-08-03", departments=day1),
        _bulletin(day="2026-08-04", departments=day1),
    )
    assert len(stable) == 1
    assert stable[0].area_desc == "Ardennes, Aube"

    churned = await _fetch(
        _bulletin(day="2026-08-03", departments=day1),
        _bulletin(day="2026-08-04", departments=day1 + (("FR712", "Cantal"),)),
    )
    assert len(churned) == 2


# --- finished days, duplicates, and other senders -------------------------


async def test_finished_run_does_not_collide_with_upcoming():
    # A finished run and an upcoming run share the day-free id, so without the
    # finished-drop they collide and AlertStore keeps only one of them.
    alerts = await _fetch(
        _bulletin(day="2026-08-01"),
        _bulletin(day="2026-08-04"),
        _bulletin(day="2026-08-05"),
        regions=["FR101"],
    )
    assert len(alerts) == 1
    (episode,) = alerts
    assert episode.onset == "2026-08-04T00:00:00+02:00"
    assert [d["date"] for d in episode.episode_days] == ["2026-08-04", "2026-08-05"]


async def test_finished_meteofrance_day_still_reads_as_expired():
    # Dropping it in the provider makes it a silent disappearance, which the
    # store already handles: _infer_terminal_phase reads the past ``expires``
    # and fires incident_removed with phase "expired" (see
    # test_store_supersession's silent-disappearance case).
    from custom_components.cap_alerts.store import _infer_terminal_phase

    (finished,) = [
        a
        for a in [
            meteoalarm._warning_to_alert(_bulletin(day="2026-08-01"), "fr"),
        ]
        if a is not None
    ]
    assert _infer_terminal_phase(finished, NOW) == "expired"


async def test_same_day_duplicate_prefers_higher_severity():
    # A2 says this should not occur, so it is defensive — but resolving it by
    # ``sent`` alone would let upstream send order seat the weaker record.
    # The green message here is sent LAST.
    alerts = await _fetch(
        _bulletin(
            day="2026-08-04",
            level=ORANGE,
            event="Vigilance orange canicule",
            sent="2026-08-03T16:00:00+02:00",
            uid="real",
            # Widen so it is not mistaken for a degenerate green marker.
            expires="2026-08-05T00:00:00+02:00",
        ),
        _bulletin(
            day="2026-08-04",
            level=YELLOW,
            event="Vigilance jaune canicule",
            sent="2026-08-03T16:00:30+02:00",
            uid="weaker",
            expires="2026-08-05T00:00:00+02:00",
        ),
        regions=["FR101"],
    )
    assert len(alerts) == 1
    assert alerts[0].parameters["awareness_level"] == ORANGE


async def test_green_markers_still_dropped_before_merge():
    # PR #68's rule runs first, so a green marker never joins a day run,
    # never widens the window, and never contributes an episode_days entry.
    alerts = await _fetch(
        _bulletin(day="2026-08-03"),
        _bulletin(day="2026-08-04"),
        _bulletin(
            day="2026-08-05",
            level=GREEN,
            expires="2026-08-05T00:00:00+02:00",  # zero-length: expires == onset
            uid="green",
        ),
        regions=["FR101"],
    )
    assert len(alerts) == 1
    assert [d["date"] for d in alerts[0].episode_days] == ["2026-08-03", "2026-08-04"]
    assert alerts[0].expires == "2026-08-05T00:00:00+02:00"  # 08-04's own expiry


async def test_non_meteofrance_alerts_pass_through_merge_unchanged():
    # Regression guard: every path is gated on the MeteoFrance sender.
    german = [
        _bulletin(
            day="2026-08-03",
            sender="dwd@dwd.de",
            departments=(("DE100", "Berlin"),),
            event="STURMBOEEN",
            uid="de1",
        ),
        _bulletin(
            day="2026-08-04",
            sender="dwd@dwd.de",
            departments=(("DE100", "Berlin"),),
            event="STURMBOEEN",
            uid="de2",
        ),
    ]
    alerts = await _fetch(*german)
    assert len(alerts) == 2
    assert all(a.episode_days == () for a in alerts)
    # Identity stays the per-message identifier hash.
    import hashlib

    for a in alerts:
        assert a.id == hashlib.sha256(a.identifier.encode()).hexdigest()[:12]


async def test_merge_leaves_other_senders_in_a_mixed_feed():
    alerts = await _fetch(
        _bulletin(day="2026-08-03"),
        _bulletin(day="2026-08-04"),
        _bulletin(
            day="2026-08-03",
            sender="dwd@dwd.de",
            departments=(("DE100", "Berlin"),),
            uid="de-mixed",
        ),
    )
    assert len(alerts) == 2
    senders = {a.sender for a in alerts}
    assert senders == {MF, "dwd@dwd.de"}
    (german,) = [a for a in alerts if a.sender == "dwd@dwd.de"]
    assert german.episode_days == ()


@pytest.mark.parametrize("regions", [None, ["FR101"]])
async def test_episode_single_day_gets_window_free_id(regions):
    from custom_components.cap_alerts.conventions import episode_id

    alerts = await _fetch(_bulletin(day="2026-08-04"), regions=regions)
    (only,) = alerts
    assert only.episode_days == ()  # nothing merged, so nothing to profile
    assert only.id == episode_id(MF, "5", ["FR101"], "", fallback="unused")
