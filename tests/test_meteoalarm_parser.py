"""MeteoAlarm JSON warnings feed parsing."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from custom_components.cap_alerts.normalize import normalize_alerts

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _REPO_ROOT / "custom_components" / "cap_alerts"
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def _ensure_update_coordinator_stub() -> None:
    """Provide ``UpdateFailed`` even when other tests stubbed HA submodules.

    ``test_geometry_endpoint`` replaces ``homeassistant.helpers`` with a bare
    module which removes ``update_coordinator``. When pytest collects tests
    in that order, the meteoalarm provider import fails. Inject a minimal
    stub so the provider loads either way.
    """
    helpers = sys.modules.get("homeassistant.helpers")
    if helpers is None:
        return
    if hasattr(helpers, "update_coordinator"):
        return
    uc = types.ModuleType("homeassistant.helpers.update_coordinator")

    class UpdateFailed(Exception):
        """Test stub of homeassistant.helpers.update_coordinator.UpdateFailed."""

    class CoordinatorEntity:
        """Test stub of homeassistant.helpers.update_coordinator.CoordinatorEntity."""

    CoordinatorEntity.__class_getitem__ = classmethod(  # type: ignore[attr-defined]
        lambda cls, _i: cls
    )
    uc.UpdateFailed = UpdateFailed
    uc.CoordinatorEntity = CoordinatorEntity
    sys.modules["homeassistant.helpers.update_coordinator"] = uc
    helpers.update_coordinator = uc  # type: ignore[attr-defined]


def _load_meteoalarm():
    """Load the MeteoAlarm provider module by file path.

    The integration's package ``__init__`` imports HA platforms; tests bypass
    that by loading the module file directly. ``model`` and ``const`` are
    preloaded so the relative imports inside the provider resolve.
    """
    full = "cap_alerts.providers.meteoalarm"
    if full in sys.modules:
        return sys.modules[full]

    if "cap_alerts" not in sys.modules:
        parent = types.ModuleType("cap_alerts")
        parent.__path__ = [str(_PKG_DIR)]
        sys.modules["cap_alerts"] = parent

    if "cap_alerts.const" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "cap_alerts.const", _PKG_DIR / "const.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["cap_alerts.const"] = mod
        spec.loader.exec_module(mod)

    if "cap_alerts.providers" not in sys.modules:
        providers_pkg = types.ModuleType("cap_alerts.providers")
        providers_pkg.__path__ = [str(_PKG_DIR / "providers")]
        sys.modules["cap_alerts.providers"] = providers_pkg

    _ensure_update_coordinator_stub()

    spec = importlib.util.spec_from_file_location(
        full, _PKG_DIR / "providers" / "meteoalarm.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


meteoalarm = _load_meteoalarm()


@pytest.fixture
def feed_de() -> dict:
    return json.loads((_FIXTURE_DIR / "meteoalarm_de.json").read_text(encoding="utf-8"))


@pytest.fixture
def feed_with_polygons() -> dict:
    return json.loads(
        (_FIXTURE_DIR / "meteoalarm_with_polygons.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def feed_no() -> dict:
    return json.loads((_FIXTURE_DIR / "meteoalarm_no.json").read_text(encoding="utf-8"))


@pytest.fixture
def feed_fr() -> dict:
    return json.loads((_FIXTURE_DIR / "meteoalarm_fr.json").read_text(encoding="utf-8"))


@pytest.fixture
def feed_fr_reissue() -> dict:
    return json.loads(
        (_FIXTURE_DIR / "meteoalarm_fr_reissue.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def feed_fr_multiday() -> dict:
    return json.loads(
        (_FIXTURE_DIR / "meteoalarm_fr_multiday.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def feed_fr_regionset_churn() -> dict:
    return json.loads(
        (_FIXTURE_DIR / "meteoalarm_fr_regionset_churn.json").read_text(
            encoding="utf-8"
        )
    )


@pytest.fixture
def feed_fr_green_markers() -> dict:
    return json.loads(
        (_FIXTURE_DIR / "meteoalarm_fr_green_markers.json").read_text(encoding="utf-8")
    )


def _parse(feed: dict, preferred_prefix: str = "de"):
    """Run the provider's per-warning conversion across a JSON payload."""
    alerts = []
    for warning in feed["warnings"]:
        alert = meteoalarm._warning_to_alert(warning, preferred_prefix)
        if alert is not None:
            alerts.append(alert)
    return alerts


def test_parses_three_active_warnings(feed_de):
    alerts = _parse(feed_de)
    # Fixture has 3 actual warnings plus one Test status that is filtered.
    assert len(alerts) == 3
    for a in alerts:
        assert a.provider == "meteoalarm"


def test_identifier_hashing_stable(feed_de):
    a1 = _parse(feed_de)
    a2 = _parse(feed_de)
    assert [a.id for a in a1] == [a.id for a in a2]
    for a in a1:
        assert len(a.id) == 12
        assert all(c in "0123456789abcdef" for c in a.id)


def test_severity_passthrough_for_normalization(feed_de):
    alerts = _parse(feed_de)
    gusts = next(a for a in alerts if a.event == "STURMBÖEN")
    assert gusts.severity == "Moderate"
    # CAP severity reaches normalize, but the awareness_level color (orange)
    # is the canonical signal and wins over CAP "Moderate".
    (out,) = normalize_alerts([gusts])
    assert out.severity_normalized == "severe"


def test_language_merge_de_primary_en_alt(feed_de):
    alerts = _parse(feed_de, preferred_prefix="de")
    gusts = next(a for a in alerts if a.event == "STURMBÖEN")
    assert gusts.language.startswith("de")
    assert gusts.headline.startswith("Amtliche")
    assert gusts.headline_alt
    assert gusts.language_alt.startswith("en")
    assert "GALE-FORCE" in gusts.headline_alt.upper()


def test_language_merge_en_primary_when_preferred_missing(feed_de):
    alerts = _parse(feed_de, preferred_prefix="fr")
    gusts = next(a for a in alerts if a.event == "gale-force gusts")
    # No fr in fixture; falls back to en (the generic English fallback rule).
    assert gusts.language.startswith("en")


@pytest.mark.parametrize("preferred_prefix", ["nb", "nn", "no"])
def test_norwegian_ha_locales_read_the_no_block(feed_no, preferred_prefix):
    # met.no tags its Norwegian blocks ``no``, a language Home Assistant does
    # not have — it offers ``nb`` and ``nn``, and ``auto`` resolves to one of
    # them on a Norwegian install. Without the equivalence group all three
    # alerts came back in English (issue #79).
    alerts = _parse(feed_no, preferred_prefix=preferred_prefix)
    assert len(alerts) == 3
    lightning = next(a for a in alerts if a.event == "Mye lyn")
    assert lightning.language == "no"
    assert lightning.headline.startswith("Mye lyn")
    # The English block is still carried as the alternate.
    assert lightning.language_alt == "en-GB"
    assert lightning.headline_alt.startswith("Frequent lightning")


def test_non_norwegian_language_still_falls_back_to_english(feed_no):
    # The group is Norwegian-only: an unrelated locale must not be widened
    # into it.
    alerts = _parse(feed_no, preferred_prefix="nl")
    lightning = next(a for a in alerts if a.event == "Frequent lightning")
    assert lightning.language == "en-GB"


def test_no_geometry_when_polygon_absent(feed_de):
    # The DE fixture's warnings carry geocodes only — no polygons.
    for a in _parse(feed_de):
        assert a.geometry is None


def test_geometry_populated_when_polygon_present(feed_with_polygons):
    alerts = _parse(feed_with_polygons, preferred_prefix="en")
    by_event = {a.event: a for a in alerts}
    triangle = by_event["Test Triangle"]
    assert triangle.geometry is not None
    assert triangle.geometry["type"] == "Polygon"
    # Single ring with 4 coordinate pairs (closed triangle).
    assert len(triangle.geometry["coordinates"]) == 1
    assert len(triangle.geometry["coordinates"][0]) == 4

    multi = by_event["Two Areas"]
    assert multi.geometry is not None
    assert multi.geometry["type"] == "MultiPolygon"
    assert len(multi.geometry["coordinates"]) == 2

    nopoly = by_event["No Polygon"]
    assert nopoly.geometry is None


def test_emma_geocodes_collected(feed_de):
    alerts = _parse(feed_de)
    gusts = next(a for a in alerts if a.event == "STURMBÖEN")
    # MeteoAlarm no longer mislabels EMMA_ID as SAME (#24); codes live in the
    # scheme-keyed ``geocodes`` container instead.
    assert gusts.geocode_same == ()
    emma = gusts.geocodes.get("EMMA_ID", ())
    assert emma
    for code in emma:
        assert code.startswith("DE")


def test_parsed_geocodes_container_is_immutable(feed_de):
    # The container is built by the shared ``geocodes_from`` funnel, so it is
    # read-only on a frozen alert — pins MeteoAlarm to that one path.
    alerts = _parse(feed_de)
    gusts = next(a for a in alerts if a.event == "STURMBÖEN")
    with pytest.raises(TypeError):
        gusts.geocodes["EMMA_ID"] = ("nope",)


def test_scheme_geocodes_multi_scheme(feed_de):
    # The DE fixture's areas carry EMMA_ID *and* WARNCELLID; both land in the
    # scheme-keyed container, keyed by their valueName.
    alerts = _parse(feed_de)
    gusts = next(a for a in alerts if a.event == "STURMBÖEN")
    assert "EMMA_ID" in gusts.geocodes
    assert "WARNCELLID" in gusts.geocodes
    for code in gusts.geocodes["EMMA_ID"]:
        assert code.startswith("DE")


def test_nuts3_feed_populates_geocodes_not_geocode_same(feed_fr):
    # France publishes NUTS3 department codes and no polygons — the #25 bug.
    alerts = _parse(feed_fr, preferred_prefix="fr")
    assert alerts
    for a in alerts:
        assert a.geocode_same == ()
        assert a.geometry is None
        assert "NUTS3" in a.geocodes
        for code in a.geocodes["NUTS3"]:
            assert code.startswith("FR")
    multi = next(a for a in alerts if a.event == "Vent violent" and "," in a.area_desc)
    assert set(multi.geocodes["NUTS3"]) == {"FR614", "FR611"}


def test_awareness_level_in_parameters(feed_de):
    alerts = _parse(feed_de)
    gusts = next(a for a in alerts if a.event == "STURMBÖEN")
    assert gusts.parameters is not None
    assert gusts.parameters.get("awareness_level", "").startswith("3;")


def test_test_status_warning_filtered(feed_de):
    # Fixture includes a status="Test" warning; ensure it's not in the output.
    events = {a.event for a in _parse(feed_de)}
    assert "Test Hazard" not in events


def test_area_desc_joined_across_areas(feed_de):
    alerts = _parse(feed_de)
    # The fixture trims each warning to two areas; joined string contains both.
    gusts = next(a for a in alerts if a.event == "STURMBÖEN")
    assert "," in gusts.area_desc or len(gusts.geocodes.get("EMMA_ID", ())) == 1


def test_repeated_parameters_joined(feed_de):
    # The DE fixture's gusts warning has two ``impacts`` parameter entries —
    # the provider joins repeats with "; " rather than dropping them.
    alerts = _parse(feed_de)
    gusts = next(a for a in alerts if a.event == "STURMBÖEN")
    assert gusts.parameters is not None
    impacts = gusts.parameters.get("impacts", "")
    assert ";" in impacts


# --- MeteoFrance identity stability (issue #37) ---------------------------


def test_reissue_same_id(feed_fr_reissue):
    # Two re-issues of the same logical MeteoFrance warning for one forecast
    # day: same sender/awareness_type/NUTS3/onset-date but different
    # identifier/sent/expires. Both must resolve to one stable id (#37).
    first, second = _parse(feed_fr_reissue, preferred_prefix="fr")
    assert first.identifier != second.identifier
    assert first.id == second.id


def test_escalation_keeps_id(feed_fr_reissue):
    # The re-issue also escalates yellow → orange; awareness_level is excluded
    # from the key, so the id is unchanged (entity updates, not spawns).
    first, second = _parse(feed_fr_reissue, preferred_prefix="fr")
    assert first.parameters["awareness_level"].startswith("2;")
    assert second.parameters["awareness_level"].startswith("3;")
    assert first.id == second.id


def test_distinct_forecast_day_distinct_id(feed_fr_multiday):
    # Same department + phenomenon, different onset dates (J vs J+1) → distinct
    # parse-level ids. This covers per-warning parsing only: the episode merge
    # runs afterwards and collapses consecutive days into one entity with a
    # day-free id (see test_meteoalarm_episodes).
    j, j1 = _parse(feed_fr_multiday, preferred_prefix="fr")
    assert j.onset[:10] != j1.onset[:10]
    assert j.id != j1.id


def test_distinct_phenomenon_distinct_id(feed_fr):
    # Same region+day but different awareness_type must stay distinct. The FR
    # fixture's Correze warning (Thunderstorm) vs a Gironde wind warning differ
    # in both, so instead assert two same-day warnings with different
    # awareness_type get different ids.
    alerts = _parse(feed_fr, preferred_prefix="fr")
    wind = next(a for a in alerts if a.event == "Vent violent" and "," in a.area_desc)
    storm = next(a for a in alerts if a.event == "Orages")
    assert wind.id != storm.id


def test_distinct_region_distinct_id(feed_fr):
    # Two "Vent violent" warnings for different departments (FR611/FR614 vs
    # FR615) must be distinct entities.
    alerts = _parse(feed_fr, preferred_prefix="fr")
    multi = next(a for a in alerts if a.event == "Vent violent" and "," in a.area_desc)
    single = next(
        a for a in alerts if a.event == "Vent violent" and "," not in a.area_desc
    )
    assert multi.id != single.id


def test_fallback_to_event_when_no_awareness_type(feed_fr):
    # The FR fixture's Gironde warning carries awareness_level but no
    # awareness_type; the phenomenon key falls back to the casefolded event and
    # still yields a stable 12-hex id.
    alerts = _parse(feed_fr, preferred_prefix="fr")
    single = next(
        a for a in alerts if a.event == "Vent violent" and "," not in a.area_desc
    )
    assert single.parameters.get("awareness_type") is None
    assert len(single.id) == 12
    assert all(c in "0123456789abcdef" for c in single.id)


def test_non_meteofrance_sender_uses_identifier_hash(feed_de):
    # Regression guard: authorities other than MeteoFrance keep the
    # per-message identifier hash byte-for-byte (dispatch default).
    alerts = _parse(feed_de)
    for a in alerts:
        assert a.sender != "vigilance@meteo.fr"
        expected = hashlib.sha256(a.identifier.encode()).hexdigest()[:12]
        assert a.id == expected


def test_region_filter_is_pure_filtering(feed_fr_regionset_churn):
    # _filter_by_regions no longer rewrites MeteoFrance ids: identity is owned
    # by the episode merge, which runs after it and recomputes from the
    # exploded single-department scope. This asserts the filter keeps the
    # matching alert byte-identical, for every sender.
    # Scope stability across a churning department set is covered end-to-end by
    # test_meteoalarm_episodes.test_episode_stable_across_footprint_churn.
    a, b = _parse(feed_fr_regionset_churn, preferred_prefix="fr")
    for alert in (a, b):
        (kept,) = meteoalarm.MeteoAlarmProvider._filter_by_regions([alert], ["FR623"])
        assert kept == alert


def test_non_meteofrance_region_filter_leaves_id_unchanged(feed_de):
    # _filter_by_regions must not recompute ids for non-MeteoFrance senders.
    alerts = _parse(feed_de)
    target = next(a for a in alerts if a.geocodes.get("EMMA_ID"))
    region = target.geocodes["EMMA_ID"][0]
    (kept,) = meteoalarm.MeteoAlarmProvider._filter_by_regions([target], [region])
    assert kept.id == target.id


# --- MeteoFrance green "no warning" markers (issue #37) -------------------


def _mf_warning(
    *,
    uuid: str,
    sender: str = "vigilance@meteo.fr",
    onset: str,
    expires: str,
    level: str = "1; green; Minor",
) -> dict:
    """Minimal one-area warning, for shapes not worth a whole fixture file."""
    return {
        "uuid": uuid,
        "alert": {
            "identifier": f"identifier-{uuid}",
            "sender": sender,
            "sent": "2026-08-03T16:05:31+02:00",
            "status": "Actual",
            "msgType": "Update",
            "info": [
                {
                    "language": "fr-FR",
                    "event": "Vigilance jaune canicule",
                    "onset": onset,
                    "expires": expires,
                    "parameter": [{"valueName": "awareness_level", "value": level}],
                    "area": [
                        {
                            "areaDesc": "Paris",
                            "geocode": [{"valueName": "NUTS3", "value": "FR101"}],
                        }
                    ],
                }
            ],
        },
    }


def test_green_marker_shares_id_with_real_warning(feed_fr_green_markers):
    # The mechanism behind the bug: the zero-length green Update and the real
    # yellow Alert for the same department-day have the same awareness_type
    # and areas, and episode_id excludes severity, so they hash alike.
    # The alert store keys on id, so one silently displaces the other.
    alerts = _parse(feed_fr_green_markers, preferred_prefix="fr")
    real = next(a for a in alerts if a.parameters["awareness_level"].startswith("2;"))
    green = next(
        a
        for a in alerts
        if a.parameters["awareness_level"].startswith("1;") and a.expires == a.onset
    )
    assert real.id == green.id


def test_green_zero_length_marker_dropped(feed_fr_green_markers):
    # expires == onset: a future day boundary, so no expires-vs-now check
    # catches it. Only the onset comparison does.
    alerts = _parse(feed_fr_green_markers, preferred_prefix="fr")
    kept = meteoalarm._drop_non_warnings(alerts)
    assert not any(a.expires == a.onset for a in kept)


def test_supersede_marker_dropped(feed_fr_green_markers):
    alerts = _parse(feed_fr_green_markers, preferred_prefix="fr")
    kept = meteoalarm._drop_non_warnings(alerts)
    assert not any(a.expires < a.onset for a in kept)


def test_green_marker_never_displaces_live_warning(feed_fr_green_markers):
    # The fixture sends the green marker two seconds *after* the real bulletin,
    # so any "latest sent wins" rule would seat the non-warning here.
    alerts = _parse(feed_fr_green_markers, preferred_prefix="fr")
    kept = meteoalarm._drop_non_warnings(alerts)
    canicule = [a for a in kept if a.event == "Vigilance jaune canicule"]
    assert len(canicule) == 1
    survivor = normalize_alerts(canicule)[0]
    assert survivor.parameters["awareness_level"].startswith("2;")
    assert survivor.severity_normalized == "moderate"
    assert survivor.expires > survivor.onset


def test_live_ids_unique_after_green_drop(feed_fr_green_markers):
    alerts = _parse(feed_fr_green_markers, preferred_prefix="fr")
    assert len({a.id for a in alerts}) < len(alerts)  # collides before the drop
    kept = meteoalarm._drop_non_warnings(alerts)
    assert len({a.id for a in kept}) == len(kept) == 2


def test_non_meteofrance_degenerate_window_kept():
    # The degenerate-window convention is MeteoFrance's and is unverified for
    # other authorities, so the drop must never reach them.
    payload = {
        "warnings": [
            _mf_warning(
                uuid="de-zero-length",
                sender="dwd@dwd.de",
                onset="2026-08-04T00:00:00+02:00",
                expires="2026-08-04T00:00:00+02:00",
            )
        ]
    }
    alerts = _parse(payload, preferred_prefix="de")
    assert meteoalarm._drop_non_warnings(alerts) == alerts


def test_unparseable_window_fails_open():
    # A feed format change must never silently drop real warnings.
    payload = {
        "warnings": [
            _mf_warning(
                uuid="fr-bad-window",
                onset="2026-08-04T00:00:00+02:00",
                expires="not-a-timestamp",
            ),
            _mf_warning(
                uuid="fr-no-window",
                onset="",
                expires="",
            ),
        ]
    }
    alerts = _parse(payload, preferred_prefix="fr")
    assert len(meteoalarm._drop_non_warnings(alerts)) == len(alerts) == 2


def test_naive_and_aware_timestamps_fail_open():
    # Mixed offset-aware/naive values are not comparable; keep the warning
    # rather than raising out of the fetch.
    payload = {
        "warnings": [
            _mf_warning(
                uuid="fr-mixed-tz",
                onset="2026-08-04T00:00:00",
                expires="2026-08-04T00:00:00+02:00",
            )
        ]
    }
    alerts = _parse(payload, preferred_prefix="fr")
    assert len(meteoalarm._drop_non_warnings(alerts)) == 1
