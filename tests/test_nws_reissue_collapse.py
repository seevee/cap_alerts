"""NWS non-VTEC re-issue collapse.

NWS re-transmits a product with no VTEC as a fresh ``messageType: Alert``
carrying a new ``urn:oid:`` identifier and an empty ``<references>``, leaving
the message it supersedes active until that message's own ``expires``. Hashing
the identifier therefore mints one entity per transmission. Measured on the
national feed 2026-08-06: 23 of 65 active non-VTEC alerts were surplus
re-issues, the deepest cluster six messages of one Air Quality Alert.

The payloads here are built rather than kept as fixtures, matching
``test_meteoalarm_fmi_episodes``: each differs from the next by exactly one
key component, and a builder keeps that difference legible.
"""

from __future__ import annotations

from datetime import datetime, timezone

from custom_components.cap_alerts.conventions import (
    collapse_nws_reissues,
    conventions_for,
)
from custom_components.cap_alerts.model import CAPAlert, geocodes_from
from custom_components.cap_alerts.providers.nws import _parse_feature

NOW = datetime(2026, 8, 7, 5, 0, tzinfo=timezone.utc)

# The stage reads nothing from its context today; it is passed for the
# signature the pipeline slot requires.
CTX = type("Ctx", (), {"now": NOW})()

DENVER_UGC = ("COC001", "COC005", "COC013")


def _alert(
    alert_id: str,
    *,
    sent: str,
    awips: str = "AQABOU",
    event: str = "Air Quality Alert",
    ugc: tuple[str, ...] = DENVER_UGC,
    vtec: tuple[str, ...] = (),
    expires: str = "2026-08-07T16:00:00-06:00",
) -> CAPAlert:
    """One NWS message. ``alert_id`` stands in for the URL hash the provider
    would have minted from the per-message ``urn:oid:`` identifier."""
    return CAPAlert(
        id=alert_id,
        identifier=f"urn:oid:2.49.0.1.840.0.{alert_id}.001.1",
        event=event,
        sent=sent,
        expires=expires,
        sender="w-nws.webmaster@noaa.gov",
        geocodes=geocodes_from({"UGC": ugc}) if ugc else geocodes_from({}),
        parameters={"AWIPSidentifier": [awips]} if awips else None,
        vtec=vtec,
        provider="nws",
    )


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------


def test_reissues_of_one_product_collapse_to_one_alert():
    # The reported shape: five transmissions of one CDPHE Action Day advisory,
    # identical in product, event and area, differing only in `sent`.
    alerts = [
        _alert("e04570", sent="2026-08-06T22:11:00-06:00"),
        _alert("aff52e", sent="2026-08-06T16:10:00-06:00"),
        _alert("c92cb7", sent="2026-08-06T12:11:00-06:00"),
        _alert("a83c0a", sent="2026-08-06T10:11:00-06:00"),
        _alert("fafb56", sent="2026-08-06T09:10:00-06:00"),
    ]
    assert len(collapse_nws_reissues(alerts, CTX)) == 1


def test_survivor_is_the_newest_transmission():
    # NWS returns newest-first, so a collapse that let the alert store pick by
    # id-keyed last-write-wins would keep the OLDEST. Order is varied here to
    # pin the choice on `sent` rather than on position.
    newest = _alert("newest", sent="2026-08-06T22:11:00-06:00")
    for order in (
        [newest, _alert("older", sent="2026-08-06T09:10:00-06:00")],
        [_alert("older", sent="2026-08-06T09:10:00-06:00"), newest],
    ):
        (survivor,) = collapse_nws_reissues(order, CTX)
        assert survivor.identifier == newest.identifier


def test_entity_id_survives_a_new_reissue():
    # The regression that matters downstream: an id that rolls over mid-episode
    # breaks any automation or card referencing it (the issue #37 failure).
    first_poll = collapse_nws_reissues(
        [_alert("fafb56", sent="2026-08-06T09:10:00-06:00")], CTX
    )
    second_poll = collapse_nws_reissues(
        [
            _alert("e04570", sent="2026-08-06T22:11:00-06:00"),
            _alert("fafb56", sent="2026-08-06T09:10:00-06:00"),
        ],
        CTX,
    )
    assert first_poll[0].id == second_poll[0].id
    # ...and it is not simply the per-message hash carried through.
    assert second_poll[0].id != "e04570"


def test_collapsed_alert_keeps_the_live_window():
    # No window enters the key, which is what retires a finished-but-unexpired
    # advisory: NWS stamps the superseded Wed->Thu message with a Friday
    # `expires`, so keying on the window would keep it as a second entity.
    survivor, *rest = collapse_nws_reissues(
        [
            _alert(
                "current",
                sent="2026-08-06T22:11:00-06:00",
                expires="2026-08-07T16:00:00-06:00",
            ),
            _alert(
                "superseded",
                sent="2026-08-06T12:11:00-06:00",
                expires="2026-08-07T09:00:00-06:00",
            ),
        ],
        CTX,
    )
    assert not rest
    assert survivor.expires == "2026-08-07T16:00:00-06:00"


# ---------------------------------------------------------------------------
# What must NOT collapse
# ---------------------------------------------------------------------------


def test_distinct_area_sets_stay_distinct():
    # The sampled feed carried two live AQABOU groups over different county
    # sets; collapsing on office and event alone would drop one.
    alerts = [
        _alert("front-range", sent="2026-08-06T22:11:00-06:00", ugc=DENVER_UGC),
        _alert(
            "western",
            sent="2026-08-06T22:11:00-06:00",
            ugc=("COC077", "COC085"),
        ),
    ]
    collapsed = collapse_nws_reissues(alerts, CTX)
    assert len({a.id for a in collapsed}) == 2


def test_distinct_events_under_one_product_stay_distinct():
    alerts = [
        _alert("aqa", sent="2026-08-06T22:11:00-06:00", event="Air Quality Alert"),
        _alert(
            "sps", sent="2026-08-06T22:11:00-06:00", event="Special Weather Statement"
        ),
    ]
    assert len(collapse_nws_reissues(alerts, CTX)) == 2


def test_vtec_alerts_pass_through_untouched():
    # VTEC is itself a supersession protocol and `_compute_alert_id` already
    # keys on its event identity tuple, so the collapse must not touch it.
    vtec = _alert(
        "heat",
        sent="2026-08-06T13:42:00-06:00",
        awips="HWOBOU",
        event="Heat Advisory",
        vtec=("/O.CON.KBOU.HT.Y.0006.260808T1700Z-260809T0200Z/",),
    )
    (survivor,) = collapse_nws_reissues([vtec], CTX)
    assert survivor is vtec


def test_two_vtec_revisions_are_left_for_vtec_identity():
    # Same product, same area, two revisions — VTEC identity already merges
    # these, and the collapse must not pre-empt it by picking one.
    revisions = [
        _alert(
            f"rev{n}",
            sent=f"2026-08-06T1{n}:00:00-06:00",
            event="Heat Advisory",
            vtec=("/O.CON.KBOU.HT.Y.0006.260808T1700Z-260809T0200Z/",),
        )
        for n in (2, 3)
    ]
    assert len(collapse_nws_reissues(revisions, CTX)) == 2


def test_degenerate_key_passes_through():
    # Neither product nor area to key on. Refusing to collapse on an unknown is
    # the fail-open direction: a duplicate entity is a nuisance, a silently
    # dropped alert is not.
    bare = _alert("bare", sent="2026-08-06T22:11:00-06:00", awips="", ugc=())
    (survivor,) = collapse_nws_reissues([bare], CTX)
    assert survivor is bare


def test_unparseable_sent_does_not_win():
    # `_ts_sort_key` sorts unparseable values last for ascending callers; under
    # `max` that would hand the group to the message nobody can date.
    good = _alert("good", sent="2026-08-06T22:11:00-06:00")
    bad = _alert("bad", sent="not-a-timestamp")
    for order in ([good, bad], [bad, good]):
        (survivor,) = collapse_nws_reissues(order, CTX)
        assert survivor.identifier == good.identifier


def test_empty_input_is_empty_output():
    assert collapse_nws_reissues([], CTX) == []


# ---------------------------------------------------------------------------
# Registration and the real payload shape
# ---------------------------------------------------------------------------


def test_stage_is_registered_on_the_merge_slot():
    # The provider runs `stages_at("merge")` after pagination completes; a
    # stage bound to any other slot would never execute.
    assert collapse_nws_reissues in conventions_for("nws").stages_at("merge")


def _feature(oid: str, sent: str, expires: str, event: str = "Air Quality Alert"):
    """A GeoJSON feature in the shape `_parse_feature` consumes."""
    return {
        "geometry": None,
        "properties": {
            "id": f"https://api.weather.gov/alerts/urn:oid:{oid}",
            "event": event,
            "sent": sent,
            "expires": expires,
            "sender": "w-nws.webmaster@noaa.gov",
            "geocode": {"UGC": list(DENVER_UGC)},
            "parameters": {"AWIPSidentifier": ["AQABOU"]},
        },
    }


def test_parsed_features_collapse_end_to_end():
    # Exercises the provider's own parse output rather than a hand-built
    # CAPAlert, so a change to how `parameters` or `geocodes` are populated
    # cannot silently decouple the key from the payload.
    parsed = [
        _parse_feature(f)
        for f in (
            _feature("a.1", "2026-08-06T22:11:00-06:00", "2026-08-07T16:00:00-06:00"),
            _feature("b.1", "2026-08-06T16:10:00-06:00", "2026-08-07T16:00:00-06:00"),
            _feature("c.1", "2026-08-06T12:11:00-06:00", "2026-08-07T09:00:00-06:00"),
        )
    ]
    # The provider mints three distinct ids from the three identifiers...
    assert len({a.id for a in parsed}) == 3
    # ...and the stage recognises them as one advisory.
    (survivor,) = collapse_nws_reissues(parsed, CTX)
    assert survivor.sent == "2026-08-06T22:11:00-06:00"
