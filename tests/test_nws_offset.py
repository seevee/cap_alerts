"""Where the NWS zone request is calculated from (#316).

``providers/nws_offset.py`` moves the centre of the request away from the true
position and keeps a record of each move, so that going back to a place gives
the request it gave before. Two properties carry the weight and are tested
over random journeys:

* the enlarged circle around the apparent centre always covers the radius
  around the true position, so no alert in range can be missed;
* a new offset is drawn only where no earlier record reaches, so travel over
  the same ground draws nothing new.

Positions are public points around New York Harbor and random points; none is
anyone's own.
"""

from __future__ import annotations

import json
import math
import random
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, rule

from custom_components.cap_alerts.providers import nws_offset
from custom_components.cap_alerts.providers.nws_offset import (
    MAX_RECORDS,
    MILE_KM,
    OFFSET_HIGH_FLOOR_KM,
    OFFSET_LOW_FLOOR_KM,
    SLACK_KM,
    TRACKER_MIN_HOLD_KM,
    LockRecord,
    LockStore,
    displaced,
    draw_offset,
    hold_km,
    offset_range_km,
)
from custom_components.cap_alerts.providers.nws_zones import haversine_km

settings.register_profile("cap_alerts", deadline=None)
settings.load_profile("cap_alerts")

BATTERY = (40.7033, -74.0170)  # Battery Park, New York
RADIUS = 100.0
PCT = 50.0
DAY = 20_000


def _store(seed: int = 1, **kwargs: Any) -> LockStore:
    return LockStore(rng=random.Random(seed), **kwargs)


def _at(store: LockStore, lat: float, lon: float, day: int = DAY, **kwargs: Any):
    kwargs.setdefault("radius_km", RADIUS)
    kwargs.setdefault("hysteresis_pct", PCT)
    return store.lock_for(lat, lon, today=day, **kwargs)


def _north(km: float, start: tuple[float, float] = BATTERY) -> tuple[float, float]:
    return displaced(start[0], start[1], km, 0.0)


def _records(store: LockStore) -> list[list[float]]:
    changed = store.changed
    rows = store.as_dict()["records"]
    store.changed = changed
    return rows


# ---------------------------------------------------------------------------
# The size of the offset
# ---------------------------------------------------------------------------


def test_the_offset_is_15_to_35_percent_of_a_large_radius():
    assert offset_range_km(400.0) == pytest.approx((60.0, 140.0))


def test_below_100_miles_the_offset_is_15_to_35_miles():
    """A percentage of a small radius hides little, so the range has a floor."""
    floor = (15 * MILE_KM, 35 * MILE_KM)
    assert (OFFSET_LOW_FLOOR_KM, OFFSET_HIGH_FLOOR_KM) == pytest.approx(floor)
    for radius in (0.0, 16.0, 100.0, 100 * MILE_KM):
        assert offset_range_km(radius) == pytest.approx(floor)
    low, high = offset_range_km(100 * MILE_KM + 10)
    assert low > floor[0]
    assert high > floor[1]


@pytest.mark.parametrize(
    "radius", [-1.0, float("nan"), float("inf"), "100", None, True]
)
def test_a_radius_that_is_not_a_usable_number_is_refused(radius):
    with pytest.raises(ValueError, match="radius_km"):
        offset_range_km(radius)
    with pytest.raises(ValueError, match="radius_km"):
        hold_km(radius, 50.0)


def test_the_hold_distance_is_the_hysteresis_share_of_the_radius():
    assert hold_km(100.0, 50.0) == 50.0
    assert hold_km(160.0, 15.0) == pytest.approx(24.0)
    assert hold_km(100.0, 0.0) == 0.0


@pytest.mark.parametrize("pct", [-0.1, 100.1, float("nan"), "50", None])
def test_a_hysteresis_outside_0_to_100_is_refused(pct):
    with pytest.raises(ValueError, match="hysteresis_pct"):
        hold_km(100.0, pct)


@given(radius=st.floats(0.0, 2000.0), seed=st.integers(0, 10_000))
def test_every_draw_lies_inside_the_range(radius, seed):
    low, high = offset_range_km(radius)
    distance, bearing = draw_offset(radius, random.Random(seed))
    assert round(low, 2) <= distance <= round(high, 2)
    assert 0.0 <= bearing < 360.0


def test_the_draw_is_uniform_by_area_not_by_distance():
    """Half the ring's area lies beyond 43.1 km of a 24.1 to 56.3 km ring. A
    draw uniform by distance would put half the draws beyond 40.2 km instead,
    and crowd the inner part of the ring."""
    rng = random.Random(7)
    low, high = offset_range_km(RADIUS)
    half_area = math.sqrt((low * low + high * high) / 2)
    draws = [draw_offset(RADIUS, rng)[0] for _ in range(20_000)]
    beyond = sum(d > half_area for d in draws) / len(draws)
    assert 0.48 < beyond < 0.52
    bearings = [draw_offset(RADIUS, rng)[1] for _ in range(20_000)]
    for quarter in range(4):
        share = sum(quarter * 90 <= b < quarter * 90 + 90 for b in bearings) / 20_000
        assert 0.23 < share < 0.27


class _Edge:
    def __init__(self, value: float) -> None:
        self.value = value

    def random(self) -> float:
        return self.value


@pytest.mark.parametrize("value", [0.0, 0.5, 1.0 - 2**-53])
def test_the_extremes_of_the_random_source_stay_in_range(value):
    low, high = offset_range_km(RADIUS)
    distance, bearing = draw_offset(RADIUS, _Edge(value))
    assert round(low, 2) <= distance <= round(high, 2)
    assert 0.0 <= bearing < 360.0


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), -0.5, 1.0, 2.0, "0.5", None, True]
)
def test_a_random_source_that_misbehaves_is_refused_and_the_store_is_untouched(value):
    """Not-a-number from the source would become the offset, and the request
    would be calculated from it. The draw is refused instead, and a store with
    remembering off keeps the record it had."""
    with pytest.raises(ValueError, match="random source"):
        draw_offset(RADIUS, _Edge(value))
    empty = LockStore(rng=_Edge(value))
    with pytest.raises(ValueError, match="random source"):
        _at(empty, *BATTERY)
    assert len(empty) == 0
    assert not empty.changed
    good = _store(remember=False)
    first = _at(good, *BATTERY)
    good._rng = _Edge(value)
    with pytest.raises(ValueError, match="random source"):
        _at(good, *_north(60.0))
    assert len(good) == 1
    assert _at(good, *BATTERY) == first


# ---------------------------------------------------------------------------
# Moving a point
# ---------------------------------------------------------------------------


@given(
    lat=st.floats(-85.0, 85.0),
    lon=st.floats(-180.0, 180.0),
    km=st.floats(0.0, 500.0),
    bearing=st.floats(0.0, 360.0),
)
def test_a_displaced_point_is_the_stated_distance_away(lat, lon, km, bearing):
    lat2, lon2 = displaced(lat, lon, km, bearing)
    assert -90.0 <= lat2 <= 90.0
    assert -180.0 <= lon2 <= 180.0
    assert haversine_km(lat, lon, lat2, lon2) == pytest.approx(km, abs=1e-6)


def test_displacement_crosses_the_antimeridian_and_the_pole():
    lat, lon = displaced(51.0, 179.9, 50.0, 90.0)
    assert lon < -179.0
    assert lat == pytest.approx(51.0, abs=0.1)
    lat, lon = displaced(89.9, 10.0, 50.0, 0.0)  # over the pole and down the far side
    assert lat == pytest.approx(89.65, abs=0.01)
    assert abs(lon) == pytest.approx(170.0, abs=0.01)


def test_displacement_due_north_and_east():
    lat, lon = displaced(40.0, -74.0, 111.195, 0.0)
    assert (lat, lon) == pytest.approx((41.0, -74.0), abs=1e-4)
    lat, lon = displaced(0.0, 0.0, 111.195, 90.0)
    assert (lat, lon) == pytest.approx((0.0, 1.0), abs=1e-4)


# ---------------------------------------------------------------------------
# The record in force
# ---------------------------------------------------------------------------


def test_the_first_position_makes_a_record_at_that_position():
    store = _store()
    lock = _at(store, *BATTERY)
    assert (lock.lat, lock.lon) == BATTERY
    assert lock.hold_km == 50.0
    low, high = offset_range_km(RADIUS)
    assert round(low, 2) <= lock.offset_km <= round(high, 2)
    assert lock.offset_max_km == round(high, 2)
    assert len(store) == 1
    assert store.changed


def test_the_apparent_centre_is_the_offset_away_from_the_true_position():
    lock = _at(_store(), *BATTERY)
    away = haversine_km(*BATTERY, *lock.centre)
    assert away == pytest.approx(lock.offset_km, abs=1e-6)
    assert away >= OFFSET_LOW_FLOOR_KM - 0.01


def test_the_record_holds_while_the_position_stays_within_the_hold_distance():
    store = _store()
    first = _at(store, *BATTERY)
    for km in (1.0, 20.0, 49.9):
        assert _at(store, *_north(km)) == first
    assert len(store) == 1


def test_leaving_the_hold_distance_makes_a_second_record():
    store = _store()
    first = _at(store, *BATTERY)
    second = _at(store, *_north(50.1))
    assert second != first
    assert second.centre != first.centre
    assert len(store) == 2


def test_going_back_takes_up_the_earlier_record_and_its_offset():
    """The reason records are kept. A fresh draw on every return would let the
    place be found from where the rings overlap."""
    store = _store()
    first = _at(store, *BATTERY)
    _at(store, *_north(60.0))
    _at(store, *_north(130.0))
    assert len(store) == 3
    for _ in range(5):
        assert _at(store, *BATTERY) == first
        _at(store, *_north(130.0))
    assert len(store) == 3


def test_a_lock_taken_on_the_way_in_stays_in_force_on_arrival():
    """Coming back from the north the lock taken 60 km out still holds at 12
    km out, where the record made at the Battery would also hold: a place can
    be seen through more than one record, each with its own fixed offset."""
    store = _store()
    home = _at(store, *BATTERY)
    out = _at(store, *_north(60.0))
    assert _at(store, *_north(12.0)) == out
    assert _at(store, *_north(9.9)) == home  # 50.1 km from the outer record
    assert len(store) == 2


def test_of_the_records_that_reach_a_position_the_one_made_first_is_taken_up():
    store = _store()
    battery = _at(store, *BATTERY)
    east_lock = _at(store, *displaced(*BATTERY, 80.0, 90.0))
    far = _at(store, *_north(400.0))
    assert len({battery, east_lock, far}) == 3
    # 45 km east of the Battery: inside the Battery record, made first, and
    # inside the east one, which is nearer.
    between = displaced(*BATTERY, 45.0, 90.0)
    assert _at(store, *between) == battery


def test_a_record_made_later_never_changes_which_one_a_spot_is_seen_through():
    """40 km north of the Battery is seen through the Battery record. A record
    made afterwards 75 km north is nearer that spot and reaches it. The spot
    must go on being seen through the Battery record, or it would have gained
    a second apparent centre."""
    store = _store()
    battery = _at(store, *BATTERY)
    far = _north(400.0)
    spot = _north(40.0)
    _at(store, *far)
    assert _at(store, *spot) == battery
    _at(store, *far)
    later = _at(store, *_north(75.0))  # out of the Battery's reach, so made new
    assert later != battery
    assert len(store) == 3
    assert haversine_km(*spot, later.lat, later.lon) < 40.0  # nearer than the Battery
    _at(store, *far)
    assert _at(store, *_north(55.0)) == later  # only the later record reaches here
    _at(store, *far)
    assert _at(store, *spot) == battery


def test_the_request_radius_is_built_from_the_largest_offset_not_the_draw():
    """Two stores draw different distances and ask about the same radius, so
    the size of the request says nothing about the draw."""
    one, two = _at(_store(1), *BATTERY), _at(_store(2), *BATTERY)
    assert one.offset_km != two.offset_km
    expected = RADIUS + round(OFFSET_HIGH_FLOOR_KM, 2) + 50.0 + SLACK_KM
    assert one.request_radius_km(RADIUS) == pytest.approx(expected)
    assert two.request_radius_km(RADIUS) == one.request_radius_km(RADIUS)


def test_a_fixed_location_keeps_one_offset_whatever_the_radius_becomes():
    store = _store()
    first = _at(store, *BATTERY, hysteresis_pct=0.0)
    assert first.hold_km == 0.0
    for radius in (16.0, 100.0, 400.0):
        assert _at(store, *BATTERY, radius_km=radius, hysteresis_pct=0.0) == first
    assert len(store) == 1
    assert (
        _at(store, *_north(0.2), hysteresis_pct=0.0) != first
    )  # the location was edited


def test_a_tracker_s_hold_is_never_shorter_than_the_minimum():
    """A phone lying still reports positions tens of metres apart. With 1% of
    a 16 km radius the hold would be 160 m, each report would leave the record
    and the same place would be drawn for again and again."""
    assert TRACKER_MIN_HOLD_KM == 8.0
    drifting = [displaced(*BATTERY, 0.2 * (i % 5), 73.0 * i) for i in range(40)]
    bare = _store()
    for lat, lon in drifting:
        _at(bare, lat, lon, radius_km=16.0, hysteresis_pct=1.0)
    assert len(bare) > 3  # drawn for the same place several times over
    held = _store()
    locks = {
        _at(
            held,
            lat,
            lon,
            radius_km=16.0,
            hysteresis_pct=1.0,
            min_hold_km=TRACKER_MIN_HOLD_KM,
        )
        for lat, lon in drifting
    }
    assert len(held) == 1
    (lock,) = locks
    assert lock.hold_km == 8.0


def test_the_minimum_does_not_shorten_a_longer_hold_and_enlarges_the_request():
    long = _at(_store(), *BATTERY, min_hold_km=TRACKER_MIN_HOLD_KM)
    assert long.hold_km == 50.0  # 50% of 100 km
    short = _at(_store(), *BATTERY, radius_km=4.0, hysteresis_pct=50.0, min_hold_km=8.0)
    assert short.hold_km == 8.0
    assert short.request_radius_km(4.0) == pytest.approx(
        4.0 + round(OFFSET_HIGH_FLOOR_KM, 2) + 8.0 + SLACK_KM
    )


@pytest.mark.parametrize("minimum", [-0.1, 161.0, float("nan"), "8", None, True])
def test_a_minimum_hold_that_is_not_usable_is_refused(minimum):
    """Above 160.9 km a record made at a small radius would not be accepted
    back when the store is read."""
    store = _store()
    with pytest.raises(ValueError, match="min_hold_km"):
        _at(store, *BATTERY, min_hold_km=minimum)
    assert len(store) == 0


def test_a_record_keeps_the_hold_it_was_made_with():
    """A change of radius or hysteresis applies to records made afterwards."""
    store = _store()
    first = _at(store, *BATTERY)  # hold 50 km
    assert _at(store, *_north(40.0), radius_km=16.0, hysteresis_pct=10.0) == first
    small = _at(store, *_north(60.0), radius_km=16.0, hysteresis_pct=10.0)
    assert small.hold_km == pytest.approx(1.6)


_STEP = st.tuples(st.floats(0.0, 360.0), st.floats(0.0, 90.0))


def _reaching(rows: list[list[float]], lat: float, lon: float) -> list[int]:
    """Every record whose radius contains the position, by a plain pass."""
    return [
        i
        for i, row in enumerate(rows)
        if haversine_km(lat, lon, row[0], row[1]) <= row[2] + SLACK_KM
    ]


@given(
    steps=st.lists(_STEP, min_size=1, max_size=40),
    start=st.tuples(st.floats(-80.0, 80.0), st.floats(-180.0, 180.0)),
    radius=st.floats(0.0, 300.0),
    pct=st.floats(0.0, 100.0),
    seed=st.integers(0, 1000),
)
@settings(max_examples=300)
def test_over_any_journey_the_request_covers_the_radius_and_nothing_is_redrawn(
    steps, start, radius, pct, seed
):
    store = _store(seed)
    lat, lon = start
    for bearing, km in steps:
        lat, lon = displaced(lat, lon, km, bearing)
        lat = max(-90.0, min(90.0, lat))
        before = _records(store)
        reached = _reaching(before, lat, lon)
        lock = _at(store, lat, lon, radius_km=radius, hysteresis_pct=pct)
        # The circle asked about contains the circle wanted.
        to_centre = haversine_km(lat, lon, *lock.centre)
        assert to_centre + radius <= lock.request_radius_km(radius) + 1e-6
        after = _records(store)
        if reached:
            # Ground already covered: nothing drawn, and the record in force
            # is one of those that reach the position.
            assert after == before
            assert [lock.lat, lock.lon] in [before[i][:2] for i in reached]
        else:
            assert len(after) == len(before) + 1
            assert after[:-1] == before


@given(
    points=st.lists(
        st.tuples(st.floats(38.0, 43.0), st.floats(-77.0, -71.0)),
        min_size=1,
        max_size=60,
    ),
    probe=st.tuples(st.floats(38.0, 43.0), st.floats(-77.0, -71.0)),
    pct=st.sampled_from([5.0, 25.0, 100.0]),
)
@settings(max_examples=200)
def test_the_record_taken_up_is_the_first_made_that_reaches(points, probe, pct):
    """The cells the search looks in must not hide an earlier record."""
    store = _store(3)
    for lat, lon in points:
        _at(store, lat, lon, hysteresis_pct=pct)
    rows = _records(store)
    far = displaced(45.0, 60.0, 0.0, 0.0)
    _at(store, *far, hysteresis_pct=0.0)  # leave every record
    rows = _records(store)[:-1]
    reached = _reaching(rows, *probe)
    lock = _at(store, *probe, hysteresis_pct=pct)
    if not reached:
        assert [lock.lat, lock.lon] not in [row[:2] for row in rows]
        return
    assert [lock.lat, lock.lon] == rows[min(reached)][:2]


@given(
    points=st.lists(
        st.tuples(st.floats(-90.0, 90.0), st.floats(-180.0, 180.0)),
        min_size=1,
        max_size=25,
    ),
    radius=st.floats(0.0, 6000.0),
    pct=st.floats(0.0, 100.0),
)
@settings(max_examples=300)
def test_anywhere_on_earth_the_record_taken_up_is_the_first_made_that_reaches(
    points, radius, pct
):
    """High latitudes, the antimeridian, holds of thousands of kilometres and
    the point opposite each record on the globe."""
    store = _store(11)
    for lat, lon in points:
        _at(store, lat, lon, radius_km=radius, hysteresis_pct=pct)
    probes = list(points)
    probes += [(-lat, lon - 180.0 if lon > 0 else lon + 180.0) for lat, lon in points]
    probes += [(90.0, 0.0), (-90.0, 0.0), (0.0, 180.0), (0.0, -180.0)]
    for lat, lon in probes:
        rows = _records(store)
        current = store.as_dict()["current"]
        store.changed = True
        reached = _reaching(rows, lat, lon)
        lock = _at(store, lat, lon, radius_km=radius, hysteresis_pct=pct)
        if not reached:
            assert len(store) == len(rows) + 1
            continue
        assert len(store) == len(rows)
        if current in reached:
            assert [lock.lat, lock.lon] == rows[current][:2]  # still held
            continue
        assert [lock.lat, lock.lon] == rows[min(reached)][:2]


def test_records_are_found_across_the_antimeridian_and_at_the_pole():
    store = _store()
    first = _at(store, 51.0, 179.99)
    _at(store, 10.0, 0.0)
    assert _at(store, 51.0, -179.99) == first
    polar = _at(store, 89.99, 0.0)
    _at(store, 10.0, 0.0)
    assert _at(store, 89.99, 180.0) == polar
    edge = _at(store, 20.0, 180.0)
    _at(store, 10.0, 0.0)
    assert _at(store, 20.0, -180.0) == edge


# ---------------------------------------------------------------------------
# A journey made five times
# ---------------------------------------------------------------------------

_CITY_HALL = (39.9526, -75.1652)  # Philadelphia City Hall, 130 km from the Battery


def _leg(
    rnd: random.Random, start: tuple[float, float], end: tuple[float, float]
) -> list[tuple[float, float]]:
    """The positions a phone reports on one drive, then while parked at the end.

    A report every 0.4 to 1.3 km, each a few metres off the line, starting at
    a different moment on every drive, so no two drives report the same
    positions. Parked, it drifts by up to 30 m.
    """
    length = haversine_km(*start, *end)
    out = []
    at = rnd.uniform(0.0, 0.6)
    while at < length:
        share = at / length
        lat = start[0] + (end[0] - start[0]) * share
        lon = start[1] + (end[1] - start[1]) * share
        out.append(displaced(lat, lon, abs(rnd.gauss(0.0, 0.008)), rnd.uniform(0, 360)))
        at += rnd.uniform(0.4, 1.3)
    out += [
        displaced(*end, rnd.uniform(0, 0.03), rnd.uniform(0, 360)) for _ in range(20)
    ]
    return out


def _five_round_trips(store: LockStore, radius: float, pct: float, minimum: float):
    """Drive Battery to City Hall and back five times.

    Returns, for each round trip, the number of records afterwards, the
    records in force on the way out in order, those on the way back, and the
    record in force while parked at each end.
    """
    rnd = random.Random(42)
    trips = []
    for trip in range(5):
        legs = []
        for start, end in ((BATTERY, _CITY_HALL), (_CITY_HALL, BATTERY)):
            seen: list[LockRecord] = []
            for lat, lon in _leg(rnd, start, end):
                lock = _at(
                    store,
                    lat,
                    lon,
                    day=DAY + trip,
                    radius_km=radius,
                    hysteresis_pct=pct,
                    min_hold_km=minimum,
                )
                if not seen or seen[-1] != lock:
                    seen.append(lock)
            legs.append(seen)
        trips.append((len(store), legs[0], legs[1], legs[0][-1], legs[1][-1]))
    return trips


_SETTINGS = [(160.9, 15.0), (100.0, 50.0), (16.0, 10.0), (5.0, 50.0)]


@pytest.mark.parametrize(("radius", "pct"), _SETTINGS)
def test_a_journey_repeated_draws_only_the_first_time(radius, pct):
    """The reason the records exist, on something like a real drive: uneven
    reports, GPS noise, a phone drifting where it is parked. After the first
    round trip no record is made, each end is seen through one record every
    time, and the way out is seen through the same records in the same order.

    The way back is not held to one order. The record taken up when a hold is
    left is the first made that reaches the position of that report, and the
    report falls somewhere different on every drive."""
    trips = _five_round_trips(_store(), radius, pct, TRACKER_MIN_HOLD_KM)
    counts = [count for count, *_ in trips]
    assert counts[0] > 1
    assert counts == [counts[0]] * 5
    made_on_the_first = set(trips[0][1]) | set(trips[0][2])
    for _count, out, back, far_end, near_end in trips:
        assert out == trips[0][1]
        assert set(back) <= made_on_the_first
        assert far_end == trips[0][3]
        assert near_end == trips[0][4]
        assert back[0] == out[-1]  # parked, then setting off: still the same record


def test_the_same_journey_with_remembering_off_draws_on_every_trip():
    """What the option costs: each end is seen through a new record, and so a
    new apparent centre, on every one of the five trips."""
    trips = _five_round_trips(_store(remember=False), 16.0, 10.0, TRACKER_MIN_HOLD_KM)
    assert len({far_end for *_, far_end, _near in trips}) == 5
    assert len({near_end for *_, near_end in trips}) == 5
    assert all(count == 1 for count, *_ in trips)


def test_the_minimum_hold_changes_the_request_far_less_often_on_a_journey():
    """A 16 km radius at 10% holds for 1.6 km, about one report in two. The
    8 km minimum changes the request a quarter as often."""
    bare = _five_round_trips(_store(), 16.0, 10.0, 0.0)
    held = _five_round_trips(_store(), 16.0, 10.0, TRACKER_MIN_HOLD_KM)
    assert len(held[0][1]) * 3 < len(bare[0][1])
    assert held[0][0] * 3 < bare[0][0]


class _Travel(RuleBasedStateMachine):
    """Any sequence of moves, jumps, changes of setting, days and restarts.

    The store is small, so that records are dropped often. After every step
    the record in force must reach the position and its request must cover
    the radius; ground already covered must not be drawn for again; and what
    is written out must read back to the same record."""

    def __init__(self) -> None:
        super().__init__()
        self.store = LockStore(rng=random.Random(9), max_records=5)
        self.at = BATTERY
        self.radius, self.pct, self.minimum, self.day = 16.0, 50.0, 0.0, DAY

    def _poll(self) -> LockRecord:
        before = _records(self.store)
        reached = _reaching(before, *self.at)
        lock = self.store.lock_for(
            *self.at,
            radius_km=self.radius,
            hysteresis_pct=self.pct,
            today=self.day,
            min_hold_km=self.minimum,
        )
        after = _records(self.store)
        assert len(after) <= 5
        away = haversine_km(*self.at, lock.lat, lock.lon)
        assert away <= lock.hold_km + SLACK_KM
        to_centre = haversine_km(*self.at, *lock.centre)
        assert to_centre + self.radius <= lock.request_radius_km(self.radius) + 1e-6
        if reached:
            assert [row[:6] for row in after] == [row[:6] for row in before]
            assert [lock.lat, lock.lon] in [before[i][:2] for i in reached]
        else:
            assert [lock.lat, lock.lon] == after[-1][:2]
        return lock

    @rule(bearing=st.floats(0, 360), km=st.floats(0, 40))
    def move(self, bearing: float, km: float) -> None:
        lat, lon = displaced(*self.at, km, bearing)
        self.at = (max(-90.0, min(90.0, lat)), lon)
        self._poll()

    @rule(lat=st.floats(-90, 90), lon=st.floats(-180, 180))
    def jump(self, lat: float, lon: float) -> None:
        self.at = (lat, lon)
        self._poll()

    @rule(
        radius=st.sampled_from([0.0, 5.0, 16.0, 100.0, 400.0]),
        pct=st.sampled_from([0.0, 10.0, 50.0, 100.0]),
        minimum=st.sampled_from([0.0, TRACKER_MIN_HOLD_KM]),
    )
    def change_the_settings(self, radius: float, pct: float, minimum: float) -> None:
        self.radius, self.pct, self.minimum = radius, pct, minimum
        self._poll()

    @rule(days=st.integers(-2, 40))
    def another_day(self, days: int) -> None:
        self.day = max(0, self.day + days)
        self._poll()

    @rule()
    def restart(self) -> None:
        lock = self._poll()
        stored = json.loads(json.dumps(self.store.as_dict()))
        self.store, problem = LockStore.from_dict(
            stored, rng=random.Random(10), max_records=5
        )
        assert problem is None
        assert self._poll() == lock


_Travel.TestCase.settings = settings(
    max_examples=60, stateful_step_count=40, deadline=None
)
test_any_sequence_of_travel_settings_and_restarts = _Travel.TestCase


# ---------------------------------------------------------------------------
# Remembering switched off
# ---------------------------------------------------------------------------


def test_with_remembering_off_only_the_record_in_force_is_kept():
    store = _store(remember=False)
    first = _at(store, *BATTERY)
    assert _at(store, *_north(30.0)) == first
    _at(store, *_north(60.0))
    assert len(store) == 1
    assert _at(store, *BATTERY) != first  # drawn again: the cost of the option
    assert len(store) == 1


# ---------------------------------------------------------------------------
# The ceiling
# ---------------------------------------------------------------------------


def test_at_the_ceiling_the_records_unused_for_longest_are_dropped():
    store = _store(max_records=40)
    locks = []
    for i in range(40):
        locks.append(_at(store, 10.0 + i, 0.0, day=DAY + i, hysteresis_pct=0.0))
    assert len(store) == 40
    _at(store, 10.0, 0.0, day=DAY + 100, hysteresis_pct=0.0)  # the oldest, used again
    newest = _at(store, 60.0, 0.0, day=DAY + 101, hysteresis_pct=0.0)
    assert len(store) == 38  # a twentieth of 40 is 2, and room for the new one
    points = [row[:2] for row in _records(store)]
    assert [locks[0].lat, 0.0] in points  # used again, so kept
    for dropped in locks[1:4]:
        assert [dropped.lat, 0.0] not in points
    assert [locks[4].lat, 0.0] in points
    assert _at(store, 60.0, 0.0, day=DAY + 101, hysteresis_pct=0.0) == newest
    assert _at(store, 10.0, 0.0, day=DAY + 102, hysteresis_pct=0.0) == locks[0]


def test_dropping_records_leaves_every_other_record_as_it_was():
    store = _store(max_records=40)
    for i in range(40):
        _at(store, 10.0 + i, 0.0, day=DAY + i, hysteresis_pct=0.0)
    before = {tuple(row[:6]): row[6] for row in _records(store)}
    _at(store, 60.0, 0.0, day=DAY + 50, hysteresis_pct=0.0)
    after = _records(store)
    assert len(after) == 38
    for row in after[:-1]:
        assert before[tuple(row[:6])] == row[6]
    assert store.as_dict()["current"] == 37


def test_the_record_just_made_is_kept_even_when_the_clock_has_gone_back():
    """The day number orders records by last use. A clock set back makes the
    newest record look the oldest; it is the one in force and must stay."""
    store = _store(max_records=3)
    for i in range(3):
        _at(store, 10.0 + i, 0.0, day=DAY + 10, hysteresis_pct=0.0)
    newest = _at(store, 60.0, 0.0, day=DAY, hysteresis_pct=0.0)
    assert len(store) == 2  # one dropped to make room, as a store this small does
    assert _at(store, 60.0, 0.0, day=DAY, hysteresis_pct=0.0) == newest
    assert [60.0, 0.0] in [row[:2] for row in _records(store)]


def test_a_store_of_one_keeps_the_newest():
    store = _store(max_records=1)
    _at(store, 10.0, 0.0, hysteresis_pct=0.0)
    second = _at(store, 20.0, 0.0, hysteresis_pct=0.0)
    assert len(store) == 1
    assert _at(store, 20.0, 0.0, hysteresis_pct=0.0) == second


@pytest.mark.parametrize("ceiling", [0, -1, MAX_RECORDS + 1, 2.5, "10", None, True])
def test_a_ceiling_outside_1_to_the_limit_is_refused(ceiling):
    with pytest.raises(ValueError, match="max_records"):
        LockStore(max_records=ceiling)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _journey(store: LockStore) -> list[LockRecord]:
    return [_at(store, *_north(km)) for km in (0.0, 60.0, 130.0, 60.0, 0.0, 200.0)]


def test_a_stored_and_reloaded_store_gives_the_same_requests():
    store = _store()
    before = _journey(store)
    text = json.dumps(store.as_dict())
    loaded, problem = LockStore.from_dict(json.loads(text), rng=random.Random(99))
    assert problem is None
    assert not loaded.changed
    assert len(loaded) == len(store) == 4
    # Still in force where it was, then the same records on the same ground.
    assert _at(loaded, *_north(200.0)) == before[-1]
    assert _journey(loaded) == before
    assert len(loaded) == 4
    assert [lock.centre for lock in _journey(loaded)] == [
        lock.centre for lock in before
    ]


@given(
    radius=st.floats(0.0, 20_000.0),
    pct=st.floats(0.0, 100.0),
    lat=st.floats(-90.0, 90.0),
    lon=st.floats(-180.0, 180.0),
    seed=st.integers(0, 10_000),
    minimum=st.one_of(st.just(0.0), st.just(8.0), st.floats(0.0, 160.9)),
)
@settings(max_examples=400)
def test_every_record_this_module_makes_is_one_it_accepts_back(
    radius, pct, lat, lon, seed, minimum
):
    """The loader refuses records that could not have been made. It must not
    refuse any that were."""
    store = _store(seed)
    lock = _at(
        store, lat, lon, radius_km=radius, hysteresis_pct=pct, min_hold_km=minimum
    )
    assert lock.hold_km >= round(minimum, 2)
    loaded, problem = LockStore.from_dict(json.loads(json.dumps(store.as_dict())))
    assert problem is None
    assert len(loaded) == 1
    assert _at(loaded, lat, lon, radius_km=radius, hysteresis_pct=pct) == lock


def test_what_is_stored_is_seven_numbers_a_record_and_under_50_bytes():
    """A position is kept to five decimal places, about a metre, which is
    what holds a record to 48 bytes in the file."""
    store = _store()
    _journey(store)
    lock = _at(store, 40.123456789, -74.987654321)
    assert (lock.lat, lock.lon) == (40.12346, -74.98765)
    assert _at(store, 40.123456789, -74.987654321, hysteresis_pct=0.0) == lock
    _at(store, *_north(200.0))
    stored = store.as_dict()
    for row in stored["records"]:
        assert row[:2] == [round(row[0], 5), round(row[1], 5)]
    assert stored["version"] == 1
    assert stored["current"] == 3
    assert all(len(row) == 7 for row in stored["records"])
    assert isinstance(stored["records"][0][6], int)
    text = json.dumps(stored["records"], separators=(",", ":"))
    assert len(text) / len(stored["records"]) < 50


def test_changed_says_when_there_is_something_to_write():
    store = _store()
    assert not store.changed
    _at(store, *BATTERY)
    assert store.changed
    store.as_dict()
    assert not store.changed
    _at(store, *_north(10.0))  # same record, same day
    assert not store.changed
    _at(store, *_north(10.0), day=DAY + 1)  # same record, a later day
    assert store.changed
    store.as_dict()
    _at(store, *_north(60.0), day=DAY + 1)  # a new record
    assert store.changed
    store.as_dict()
    _at(store, *BATTERY, day=DAY + 1)  # an earlier record taken up
    assert store.changed


def test_nothing_stored_is_an_empty_store_with_nothing_to_report():
    store, problem = LockStore.from_dict(None)
    assert problem is None
    assert len(store) == 0
    assert not store.changed


def test_a_store_written_with_remembering_on_is_cut_to_the_record_in_force():
    store = _store()
    before = _journey(store)
    loaded, problem = LockStore.from_dict(store.as_dict(), remember=False)
    assert problem is None
    assert len(loaded) == 1
    assert loaded.changed
    assert _at(loaded, *_north(200.0)) == before[-1]


_GOOD = [40.7033, -74.017, 50.0, 30.0, 123.4, 56.33, DAY]


@pytest.mark.parametrize(
    "stored",
    [
        "text",
        [],
        7,
        {},
        {"version": 2, "records": [_GOOD]},
        {"version": "1", "records": [_GOOD]},
        {"version": True, "records": [_GOOD]},
        {"version": 1.0, "records": [_GOOD]},
        {"version": None, "records": [_GOOD]},
        {"records": [_GOOD]},
        {"version": 1},
        {"version": 1, "records": "none"},
        {"version": 1, "records": {"0": _GOOD}},
    ],
)
def test_a_file_that_is_not_a_store_gives_an_empty_one_and_says_so(stored):
    store, problem = LockStore.from_dict(stored)
    assert len(store) == 0
    assert problem is not None
    assert "starting empty" in problem


def _with(position: int, value: Any) -> list[Any]:
    row: list[Any] = list(_GOOD)
    row[position] = value
    return row


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "record",
        {},
        _GOOD[:6],
        [*_GOOD, 1],
        _with(0, 90.1),
        _with(0, "40.7"),
        _with(0, None),
        _with(0, True),
        _with(0, float("nan")),
        _with(1, -180.1),
        _with(1, float("inf")),
        _with(1, 10**400),
        _with(2, -1.0),
        _with(2, 30_000.0),
        _with(3, 60.0),  # an offset larger than the largest possible
        _with(3, -1.0),
        _with(4, 360.5),
        _with(4, -0.1),
        _with(5, 30_000.0),
        _with(6, -1),
        _with(6, 1.5),
        _with(6, 10**12),
        _with(6, [DAY]),
        [40.7033, -74.017, 0, 0, 0, 0, 0],  # a run of zeros: no offset at all
        [0, 0, 0, 0, 0, 0, 0],
        _with(3, 0.0),
        _with(3, 1.0),  # an offset smaller than any that could be drawn
        _with(3, 24.12),
        [40.7033, -74.017, 50.0, 30.0, 123.4, 56.32, DAY],  # a limit below the floor
        [40.7033, -74.017, 50.0, 30.0, 123.4, 30.0, DAY],
        [40.7033, -74.017, 300.0, 59.0, 123.4, 140.0, DAY],  # below 15% at 400 km
        _with(2, 161.1),  # a hold longer than the radius the limit belongs to
        _with(2, 15_000.0),
    ],
)
def test_a_record_that_cannot_be_read_is_left_out_and_reported(bad):
    """The readable records are kept: each is a place not drawn again."""
    stored = {"version": 1, "current": 2, "records": [_GOOD, bad, _with(0, 41.5)]}
    store, problem = LockStore.from_dict(stored, rng=random.Random(1))
    assert len(store) == 2
    assert problem == "1 unreadable record(s) left out"
    assert store.changed
    lock = _at(store, 41.5, -74.017)  # the record in force, at its new position
    assert (lock.lat, lock.offset_km, lock.bearing_deg) == (41.5, 30.0, 123.4)
    assert len(store) == 2


def test_a_damaged_record_with_no_offset_is_never_used_as_it_stands():
    """The defect this guards against raised no error. A file of zeros reads
    as a record at the true position with an offset of zero, and the request
    would have been calculated from the true position itself."""
    zeroed = [40.7033, -74.017, 0, 0, 0, 0, 0]
    store, problem = LockStore.from_dict(
        {"version": 1, "current": 0, "records": [zeroed]}, rng=random.Random(1)
    )
    assert len(store) == 0
    assert problem is not None
    assert "1 unreadable" in problem
    lock = _at(store, 40.7033, -74.017)
    assert lock.offset_km >= OFFSET_LOW_FLOOR_KM - 0.01
    assert haversine_km(40.7033, -74.017, *lock.centre) >= OFFSET_LOW_FLOOR_KM - 0.02


@pytest.mark.parametrize(
    "edge",
    [
        _with(3, 24.14),  # the smallest offset
        _with(3, 56.33),  # the largest
        _with(2, 160.93),  # the longest hold at this limit: 100% of 100 miles
        _with(2, 0.0),
        [40.7033, -74.017, 400.0, 60.0, 0.0, 140.0, 0],
        [40.7033, -74.017, 400.0, 140.0, 360.0, 140.0, 10_000_000],
        [90, -180, 50, 30, 0, 56.33, 1],
        [-90.0, 179.99999, 50.0, 30.0, 0.0, 56.33, 1.0],
    ],
)
def test_a_record_at_the_edge_of_what_can_be_made_is_accepted(edge):
    store, problem = LockStore.from_dict(
        {"version": 1, "current": 0, "records": [edge]}
    )
    assert problem is None
    assert len(store) == 1


def test_a_stored_longitude_of_180_is_found_from_either_side():
    """The cells run from -180 up to, not including, 180. A record filed
    under 180 would be in none of them and never taken up again."""
    stored = {
        "version": 1,
        "current": 1,
        "records": [_with(1, 180.0), _with(0, 10.0)],
    }
    store, problem = LockStore.from_dict(stored)
    assert problem is None
    assert store.as_dict()["records"][0][1] == -180.0
    for lon in (180.0, -180.0, 179.9, -179.9):
        lock = _at(store, 40.7033, lon)
        assert (lock.lon, lock.offset_km) == (-180.0, 30.0)
        _at(store, 10.0, -74.017)
    assert len(store) == 2


@pytest.mark.parametrize("current", [5, -1, "0", 1.0, True, [0], 10**400])
def test_a_record_in_force_that_cannot_be_read_is_reported(current):
    stored = {"version": 1, "current": current, "records": [_GOOD]}
    store, problem = LockStore.from_dict(stored)
    assert len(store) == 1
    assert problem is not None
    assert "in force" in problem
    lock = _at(store, 40.7033, -74.017)
    assert lock.offset_km == 30.0  # taken up again, not drawn again


@pytest.mark.parametrize("current", [True, False])
def test_true_and_false_are_not_record_numbers(current):
    """In Python they equal 1 and 0, and would pick out a record."""
    stored = {"version": 1, "current": current, "records": [_GOOD, _with(0, 60.0)]}
    store, problem = LockStore.from_dict(stored)
    assert len(store) == 2
    assert problem is not None
    assert "in force" in problem


def test_the_record_in_force_being_the_unreadable_one_is_reported():
    stored = {"version": 1, "current": 1, "records": [_GOOD, _with(0, 91.0)]}
    store, problem = LockStore.from_dict(stored)
    assert len(store) == 1
    assert problem is not None
    assert "1 unreadable" in problem
    assert "in force" in problem


def test_records_beyond_the_ceiling_are_left_out_and_reported():
    rows = [_with(0, 10.0 + i) for i in range(12)]
    store, problem = LockStore.from_dict(
        {"version": 1, "current": 11, "records": rows}, max_records=10
    )
    assert len(store) == 10
    assert problem is not None
    assert "2 record(s) beyond the limit of 10" in problem
    assert "in force" in problem


_JSON = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(-(10**30), 10**30),
        st.floats(allow_nan=True, allow_infinity=True),
        st.text(max_size=8),
        st.just(1),
    ),
    lambda inner: st.one_of(
        st.lists(inner, max_size=8),
        st.dictionaries(
            st.sampled_from(["version", "current", "records", "other"]),
            inner,
            max_size=4,
        ),
    ),
    max_leaves=30,
)


@given(stored=_JSON, seed=st.integers(0, 100))
@settings(max_examples=500)
def test_whatever_is_in_the_file_loading_returns_a_usable_store(stored, seed):
    store, _problem = LockStore.from_dict(stored, rng=random.Random(seed))
    assert len(store) <= MAX_RECORDS
    lock = _at(store, *BATTERY)
    assert lock.request_radius_km(RADIUS) >= RADIUS
    again, problem = LockStore.from_dict(store.as_dict())
    assert problem is None
    assert _at(again, *BATTERY) == lock


@given(
    rows=st.lists(
        st.lists(
            st.one_of(
                st.floats(allow_nan=True, allow_infinity=True),
                st.integers(-(10**20), 10**20),
                st.sampled_from(
                    [0, 1, 40.7, -74.0, 50.0, 30.0, 56.33, 180.0, 360.0, DAY]
                ),
            ),
            min_size=6,
            max_size=8,
        ),
        max_size=12,
    ),
    current=st.one_of(st.none(), st.integers(-2, 14)),
)
@settings(max_examples=500)
def test_every_record_that_loads_is_within_the_limits(rows, current):
    stored = {"version": 1, "current": current, "records": rows}
    store, _problem = LockStore.from_dict(stored)
    for lat, lon, hold, offset, bearing, offset_max, day in store.as_dict()["records"]:
        assert -90.0 <= lat <= 90.0
        assert -180.0 <= lon < 180.0
        assert 0.0 <= hold <= 20_100.0
        assert 24.13 <= offset <= offset_max <= 20_100.0
        assert offset_max >= 56.33
        assert 0.0 <= bearing <= 360.0
        assert isinstance(day, int)
        assert day >= 0
    _at(store, *BATTERY)


# ---------------------------------------------------------------------------
# Input to the poll
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lat", "lon"),
    [
        (90.1, 0.0),
        (-90.1, 0.0),
        (0.0, 180.1),
        (0.0, -180.1),
        (float("nan"), 0.0),
        (0.0, float("inf")),
        ("40.7", -74.0),
        (None, -74.0),
        (True, -74.0),
    ],
)
def test_a_position_that_is_not_usable_is_refused_and_nothing_is_recorded(lat, lon):
    store = _store()
    with pytest.raises(ValueError, match="lat|lon"):
        _at(store, lat, lon)
    assert len(store) == 0
    assert not store.changed


@pytest.mark.parametrize("day", [-1, 1.5, 10**9, "20000", None, float("nan")])
def test_a_day_that_is_not_a_whole_number_is_refused(day):
    with pytest.raises(ValueError, match="today"):
        _at(_store(), *BATTERY, day=day)


# ---------------------------------------------------------------------------
# Two threads
# ---------------------------------------------------------------------------


def test_calls_from_several_threads_do_not_corrupt_the_store():
    """The lookups run in the executor while the event loop may be writing the
    store out. Eight threads start together and walk the same ground in the
    same order, which is the worst case: each arrives at new ground at the
    same moment. Afterwards no two records may have been drawn for ground the
    other already covered, every thread must have been given records that are
    in the store, and every copy written out must have been whole.

    The interpreter is told to switch threads as often as it can, so that an
    unguarded store fails here nearly every time and not once in several."""
    store = LockStore(rng=random.Random(5), max_records=2000)
    points = [displaced(40.0, -90.0, 11.0 * i, 3.0 * i) for i in range(300)]
    errors: list[BaseException] = []
    seen: list[tuple[float, float]] = []
    together = threading.Barrier(8)

    def walk() -> None:
        try:
            together.wait(timeout=30)
            for i, (lat, lon) in enumerate(points):
                lock = store.lock_for(
                    lat, lon, radius_km=20.0, hysteresis_pct=50.0, today=DAY
                )
                away = haversine_km(lat, lon, lock.lat, lock.lon)
                assert away <= lock.hold_km + SLACK_KM
                seen.append((lock.lat, lock.lon))
                if i % 20 == 0:
                    written = store.as_dict()
                    assert all(len(row) == 7 for row in written["records"])
                    assert written["current"] < len(written["records"])
        except BaseException as err:  # noqa: BLE001 - reported by the assertion below
            errors.append(err)

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=walk) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(interval)
    assert errors == []
    rows = store.as_dict()["records"]
    assert len(rows) == len(store) == len(points)  # 11 km apart, 10 km hold
    assert set(seen) <= {(row[0], row[1]) for row in rows}
    for i, row in enumerate(rows):
        for other in rows[:i]:
            # Made later, so not inside the earlier record's radius.
            assert haversine_km(row[0], row[1], other[0], other[1]) > other[2]


def test_the_module_imports_nothing_from_home_assistant():
    source = Path(nws_offset.__file__).read_text(encoding="utf-8")
    assert "homeassistant" not in source


# ---------------------------------------------------------------------------
# A saved file that no longer reads as one
# ---------------------------------------------------------------------------


def _saved(count: int = 12, seed: int = 9) -> tuple[list[list[float]], str]:
    """The records of a store with ``count`` of them, and the file's text."""
    store = _store(seed)
    for step in range(count):
        store.lock_for(
            35.0 + 0.9 * step,
            -100.0 + 1.3 * step,
            radius_km=100.0,
            hysteresis_pct=50.0,
            today=20 + step,
        )
    document = store.as_dict()
    text = json.dumps(
        {"version": 1, "minor_version": 1, "key": "cap_alerts.x", "data": document}
    )
    return document["records"], text


def _spans(rows: list[list[float]], text: str) -> list[tuple[int, int]]:
    """Where each record is written in the text."""
    spans, start = [], 0
    for row in rows:
        at = text.index(json.dumps(row), start)
        spans.append((at, at + len(json.dumps(row))))
        start = spans[-1][1]
    return spans


def test_a_file_cut_short_anywhere_gives_back_every_record_written_whole():
    """A write that stopped part way, or a disk that lost the end: at every
    length the file could have been cut to, each record wholly before the
    cut is recovered, in the order made, and nothing raises."""
    rows, text = _saved()
    spans = _spans(rows, text)
    for cut in range(len(text) + 1):
        store, report = LockStore.salvage(text[:cut])
        whole = [row for row, (_start, end) in zip(rows, spans) if end <= cut]
        assert store.as_dict()["records"] == whole, cut
        assert f"{len(whole)} record(s) recovered" in report


@given(
    start=st.integers(0, 700),
    length=st.integers(0, 200),
    junk=st.text(max_size=60),
)
def test_a_file_overwritten_in_part_keeps_every_record_the_damage_missed(
    start, length, junk
):
    """Whatever is written over a stretch of the file, the records lying
    wholly outside the stretch come back, in order, and nothing raises."""
    rows, text = _saved()
    start = min(start, len(text))
    damaged = text[:start] + junk + text[start + length :]

    store, _report = LockStore.salvage(damaged)

    recovered = store.as_dict()["records"]
    untouched = [
        row
        for row, (begin, end) in zip(rows, _spans(rows, text))
        if end <= start or begin >= start + length
    ]
    remaining = iter(recovered)
    assert all(row in remaining for row in untouched)  # all there, in order
    for row in recovered:
        assert (
            LockStore.from_dict({"version": 1, "current": None, "records": [row]})[1]
            is None
        )


def test_what_is_recovered_is_checked_as_any_stored_record_is():
    """Text holds whatever it holds. Seven numbers in brackets that could
    not be a record made here are not taken for one."""
    rows, text = _saved(3)
    zero = "[0, 0, 0, 0, 0, 0, 0]"
    no_offset = json.dumps([40.0, -74.0, 50.0, 0.0, 10.0, 56.33, 3])
    far = json.dumps([140.0, -74.0, 50.0, 30.0, 10.0, 56.33, 3])
    huge = "[" + "9" * 5000 + ", 1, 1, 1, 1, 1, 1]"
    powers = "[1e999, -74.0, 50.0, 30.0, 10.0, 56.33, 3]"

    store, report = LockStore.salvage(zero + no_offset + text + far + huge + powers)

    assert store.as_dict()["records"] == rows
    assert "3 record(s) recovered" in report
    # Four are counted. The fifth holds a number too long to read at all.
    assert "4 unreadable record(s) left out" in report


def test_after_a_salvage_no_record_is_in_force_and_the_set_is_to_be_written():
    rows, text = _saved(4)

    store, _report = LockStore.salvage(text[:-3])

    assert store.as_dict()["current"] is None
    store, _report = LockStore.salvage(text[:-3])
    assert store.changed
    # The next position takes up the first made that reaches it, as before.
    lock = store.lock_for(35.0, -100.0, radius_km=100.0, hysteresis_pct=50.0, today=40)
    assert [lock.lat, lock.lon, lock.offset_km, lock.bearing_deg] == [
        rows[0][0],
        rows[0][1],
        rows[0][3],
        rows[0][4],
    ]
    assert len(store) == 4


@pytest.mark.parametrize(
    "text",
    [None, b"[1,2,3,4,5,6,7]", 7, ["x"], "", "{}", "\x00" * 50],
    ids=["none", "bytes", "number", "list", "empty", "braces", "zeros"],
)
def test_nothing_recoverable_gives_an_empty_store_and_says_so(text):
    store, report = LockStore.salvage(text)

    assert len(store) == 0
    assert "0 record(s) recovered" in report


def test_a_salvage_keeps_to_the_limit_and_to_the_setting():
    rows, text = _saved(12)

    few, report = LockStore.salvage(text, max_records=5)
    none, _report = LockStore.salvage(text, remember=False)

    assert few.as_dict()["records"] == rows[:5]
    assert "7 record(s) beyond the limit of 5 left out" in report
    assert len(none) == 0  # with remembering off only the record in force is kept


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("[" * 400_000, id="brackets"),
        pytest.param("[1," * 150_000, id="starts"),
        pytest.param("[" + "1" * 400_000, id="digits"),
        pytest.param("[1, 2, 3, 4, 5, 6, " * 30_000, id="six-numbers"),
        pytest.param("[1.1.1.1.1.1.1, " * 40_000, id="points"),
        pytest.param("[1e1e1e1, 2, 3, 4, 5, 6, 7]" * 20_000, id="exponents"),
    ],
)
def test_text_made_to_be_slow_to_search_is_not(text):
    """The file is read up to 8 MB. A search of it must take time in
    proportion to its length whatever it holds."""
    import time

    began = time.perf_counter()
    store, _report = LockStore.salvage(text)

    assert len(store) == 0
    assert time.perf_counter() - began < 5.0
