"""Property-based tests over the restore file's serialization (issue #281).

The example test in ``test_restore_serialization.py`` sets every field once.
Here Hypothesis varies them: empty and non-empty tuples, nested points, a
missing or present bbox, geocode containers, nullable text, so the round trip
holds for shapes no fixture carries, and the payload is always plain JSON.
"""

from __future__ import annotations

import json
from dataclasses import replace

from hypothesis import given, settings
from hypothesis import strategies as st

from custom_components.cap_alerts.model import CAPAlert, geocodes_from
from custom_components.cap_alerts.restore import alert_from_storage, alert_to_storage

# The HA test environment is slow to import and Hypothesis's per-example
# deadline is a flakiness source there, not a signal.
settings.register_profile("cap_alerts", deadline=None)
settings.load_profile("cap_alerts")

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_text = st.text(max_size=20)
_nonempty = st.text(min_size=1, max_size=12)
_maybe_text = st.none() | _text
_strs = st.lists(_text, max_size=4).map(tuple)
_coord = st.floats(allow_nan=False, allow_infinity=False, width=64)

_points = st.lists(st.tuples(_coord, _coord), max_size=4).map(tuple)
_bbox = st.none() | st.tuples(_coord, _coord, _coord, _coord)
_geocodes = st.dictionaries(
    _nonempty, st.lists(_nonempty, min_size=1, max_size=3), max_size=3
).map(geocodes_from)
_episode_days = st.lists(st.dictionaries(_nonempty, _text, max_size=4), max_size=3).map(
    tuple
)
_parameters = st.none() | st.dictionaries(
    _nonempty,
    _text | st.integers() | st.lists(_text, max_size=3),
    max_size=4,
)
_geometry = st.none() | st.fixed_dictionaries(
    {"type": st.just("Point"), "coordinates": st.lists(_coord, min_size=2, max_size=2)}
)

alerts = st.builds(
    CAPAlert,
    id=_nonempty,
    url=_text,
    event=_text,
    severity=_text,
    onset=_text,
    expires=_text,
    ends=_maybe_text,
    headline=_text,
    description=_text,
    instruction=_maybe_text,
    area_desc=_text,
    affected_zones=_strs,
    affected_zone_uris=_strs,
    geocodes=_geocodes,
    geometry=_geometry,
    geometry_ref=_text,
    bbox=_bbox,
    points=_points,
    is_marine=st.booleans(),
    vtec=_strs,
    references=_strs,
    parameters=_parameters,
    instruction_alt=_maybe_text,
    episode_days=_episode_days,
    provider=_text,
    phase=_text,
    previous_phase=_text,
    phase_changed=st.booleans(),
    stale=st.booleans(),
    last_confirmed=_text,
)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@given(alerts)
@settings(max_examples=200)
def test_round_trip_keeps_everything_but_the_excluded_fields(alert: CAPAlert) -> None:
    restored = alert_from_storage(alert_to_storage(alert))

    assert restored == replace(
        alert, geometry=None, previous_phase="", phase_changed=False
    )


@given(alerts)
@settings(max_examples=200)
def test_payload_is_plain_json(alert: CAPAlert) -> None:
    stored = alert_to_storage(alert)

    assert json.loads(json.dumps(stored)) == stored
    assert alert_from_storage(json.loads(json.dumps(stored))) == alert_from_storage(
        stored
    )
