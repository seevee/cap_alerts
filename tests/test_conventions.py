"""Convention table resolution and predicates (issue #82)."""

from datetime import datetime, timezone

import pytest

from custom_components.cap_alerts.conventions import (
    CONVENTIONS,
    FMI_EPISODES,
    FMI_SENDER,
    METEOFRANCE_EPISODES,
    METEOFRANCE_SENDER,
    SourceConventions,
    StageContext,
    conventions_for,
    is_marine_code,
    meteoalarm_awareness_severity,
    nws_vtec_severity,
)

# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_unknown_provider_gets_empty_conventions():
    # An unregistered source degrades to pure CAP handling rather than raising.
    conv = conventions_for("does-not-exist")
    assert conv.marine_code_prefixes == frozenset()
    assert conv.terminal_lifecycle_statuses == frozenset()
    assert conv.severity is None
    assert conv.classifies_marine is False


def test_sender_falls_back_to_provider_entry():
    # No sender-scoped entry exists yet, so a sender must not lose the
    # provider's conventions.
    assert conventions_for("nws", "w-nws.webmaster@noaa.gov") is CONVENTIONS["nws"]


def test_sender_scoped_entry_wins_when_present(monkeypatch):
    # The MeteoFrance case the table is shaped for: one provider, several
    # dialects. Patched over the shipped entry so the lookup is tested on its
    # own, independently of what that dialect happens to declare.
    scoped = SourceConventions(terminal_lifecycle_statuses=frozenset({"over"}))
    patched = dict(CONVENTIONS)
    patched["meteoalarm/vigilance@meteo.fr"] = scoped
    monkeypatch.setattr("custom_components.cap_alerts.conventions.CONVENTIONS", patched)
    assert conventions_for("meteoalarm", "vigilance@meteo.fr") is scoped
    assert conventions_for("meteoalarm", "other@example.org").severity is not None


# ---------------------------------------------------------------------------
# Sender dialects (issue #88)
# ---------------------------------------------------------------------------


def test_meteofrance_entry_carries_every_hook():
    # The shipped sender dialect: identity, green-marker drop, and both
    # list-shaped stages resolve ahead of the provider entry.
    conventions = conventions_for("meteoalarm", METEOFRANCE_SENDER)
    assert conventions is CONVENTIONS[f"meteoalarm/{METEOFRANCE_SENDER}"]
    assert conventions is not CONVENTIONS["meteoalarm"]
    # Restated, not inherited — a sender entry replaces the provider's rather
    # than layering on top, so a French alert must not lose awareness_level
    # severity on the way through.
    assert conventions.severity is meteoalarm_awareness_severity
    assert conventions.identity is not None
    assert conventions.keep is not None
    assert [stage.slot for stage in conventions.stages] == ["explode", "merge"]


def test_fmi_entry_declares_the_episode_stages_and_nothing_else():
    # The second episode dialect (issue #98). It declares the same two stages
    # and restates the MeteoAlarm severity derivation, but neither per-alert
    # hook: Finland publishes no green/no-warning markers to drop, and the
    # merge re-mints every shipped id, so there is nothing for `identity` to do.
    conventions = conventions_for("meteoalarm", FMI_SENDER)
    assert conventions is CONVENTIONS[f"meteoalarm/{FMI_SENDER}"]
    assert conventions.severity is meteoalarm_awareness_severity
    assert [stage.slot for stage in conventions.stages] == ["explode", "merge"]
    assert conventions.keep is None
    assert conventions.identity is None


def test_episode_dialects_share_everything_but_the_run_rule():
    # The point of declaring the dialect: two senders, one pipeline. If these
    # ever became the same predicate the second dialect would stop being data.
    assert METEOFRANCE_EPISODES.sender != FMI_EPISODES.sender
    assert METEOFRANCE_EPISODES.split is not FMI_EPISODES.split


def test_other_meteoalarm_senders_keep_the_provider_entry():
    conventions = conventions_for("meteoalarm", "dwd@dwd.de")
    assert conventions is CONVENTIONS["meteoalarm"]
    assert conventions.identity is None
    assert conventions.keep is None
    assert conventions.stages == ()


def test_stages_at_selects_by_slot():
    conventions = conventions_for("meteoalarm", METEOFRANCE_SENDER)
    (explode,) = conventions.stages_at("explode")
    (merge,) = conventions.stages_at("merge")
    assert explode is not merge
    assert conventions.stages_at("no-such-slot") == ()


def test_source_without_conventions_has_every_slot_empty():
    # What makes the provider pipeline safe to run unconditionally: an
    # unregistered source declares nothing at any hook, rather than raising.
    conventions = conventions_for("does-not-exist")
    assert conventions.identity is None
    assert conventions.keep is None
    assert conventions.stages == ()
    assert conventions.stages_at("explode") == conventions.stages_at("merge") == ()


@pytest.mark.parametrize("sender", [METEOFRANCE_SENDER, FMI_SENDER])
def test_stages_pass_foreign_senders_through_untouched(alert_factory, sender):
    # Stages are handed the whole batch, so each one has to leave alerts from
    # senders it does not own exactly as they were.
    conventions = conventions_for("meteoalarm", sender)
    alerts = [alert_factory(provider="meteoalarm", sender="dwd@dwd.de")]
    ctx = StageContext(
        now=datetime(2026, 8, 5, tzinfo=timezone.utc),
        wanted_regions=frozenset({"DE123"}),
    )
    for run in conventions.stages_at("explode") + conventions.stages_at("merge"):
        assert run(list(alerts), ctx) == alerts


# ---------------------------------------------------------------------------
# Marine classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["nws", "eccc"])
def test_marine_classifying_providers(provider):
    assert conventions_for(provider).classifies_marine is True


@pytest.mark.parametrize("provider", ["meteoalarm", "wmo"])
def test_non_marine_classifying_providers(provider):
    # Declared absence, not an oversight: these feeds publish no marine
    # discriminator, which is why the options flow withholds the toggle.
    assert conventions_for(provider).classifies_marine is False


def test_is_marine_code_empty_prefixes_is_always_false():
    conv = conventions_for("wmo")
    assert is_marine_code(("ANZ450", "004310"), conv) is False


def test_is_marine_code_matches_nws_two_char_block():
    conv = conventions_for("nws")
    assert is_marine_code(("ANZ450",), conv) is True
    assert is_marine_code(("OHC049", "GMZ650"), conv) is True
    assert is_marine_code(("OHC049", "NYZ072"), conv) is False
    assert is_marine_code((), conv) is False


def test_is_marine_code_matches_eccc_water_block():
    conv = conventions_for("eccc")
    assert is_marine_code(("004310",), conv) is True
    assert is_marine_code(("071100", "004410"), conv) is True
    assert is_marine_code(("071100",), conv) is False


def test_is_marine_code_ignores_codes_shorter_than_a_prefix():
    # Guards the prefix predicate against a truncated code being read as a
    # partial match.
    assert is_marine_code(("A",), conventions_for("nws")) is False


# ---------------------------------------------------------------------------
# Severity derivations — None means "no signal, use CAP severity"
# ---------------------------------------------------------------------------


def test_nws_vtec_severity_without_vtec_returns_none(alert_factory):
    assert nws_vtec_severity(alert_factory(provider="nws")) is None


def test_nws_vtec_severity_escalates_tornado_warning(alert_factory):
    alert = alert_factory(vtec_significance="W", vtec_phenomena="TO")
    assert nws_vtec_severity(alert) == "extreme"


def test_nws_vtec_severity_maps_significance(alert_factory):
    assert nws_vtec_severity(alert_factory(vtec_significance="A")) == "moderate"
    assert nws_vtec_severity(alert_factory(vtec_significance="Y")) == "minor"


def test_meteoalarm_awareness_severity_reads_colour_token(alert_factory):
    alert = alert_factory(parameters={"awareness_level": "3; orange; Severe"})
    assert meteoalarm_awareness_severity(alert) == "severe"


def test_meteoalarm_awareness_severity_none_when_absent_or_malformed(alert_factory):
    assert meteoalarm_awareness_severity(alert_factory(parameters=None)) is None
    assert meteoalarm_awareness_severity(alert_factory(parameters={})) is None
    assert (
        meteoalarm_awareness_severity(
            alert_factory(parameters={"awareness_level": "3"})
        )
        is None
    )
    assert (
        meteoalarm_awareness_severity(
            alert_factory(parameters={"awareness_level": "3; mauve; ?"})
        )
        is None
    )
