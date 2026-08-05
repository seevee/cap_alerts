"""Convention table resolution and predicates (issue #82)."""

import pytest

from custom_components.cap_alerts.conventions import (
    CONVENTIONS,
    SourceConventions,
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
    # dialects. Patched in rather than shipped, since migrating those rules is
    # deliberately out of scope for this pass.
    scoped = SourceConventions(terminal_lifecycle_statuses=frozenset({"over"}))
    patched = dict(CONVENTIONS)
    patched["meteoalarm/vigilance@meteo.fr"] = scoped
    monkeypatch.setattr("custom_components.cap_alerts.conventions.CONVENTIONS", patched)
    assert conventions_for("meteoalarm", "vigilance@meteo.fr") is scoped
    assert conventions_for("meteoalarm", "other@example.org").severity is not None


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
