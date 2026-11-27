"""Where the NWS zone request is calculated from, in place of the true position (#316).

The local mode asks ``alerts/active?zone=`` about every zone within the
radius. A list calculated from the exact position can be worked back to it by
anyone who holds the zone shapes: measured at six places, the area from which
an identical list is sent was 2 to 430 square kilometres. So the list is
calculated from an apparent centre instead, some distance from the true
position, and the circle is enlarged so that it still covers every point
within the radius of the true position. What lies outside the true radius is
discarded after the reply arrives.

**The offset.** A random direction and a random distance. The distance is
between 15% and 35% of the radius, and never from a range below 15 to 35
miles, since a percentage of a small radius hides little. It is drawn uniform
by area, so no part of the ring is more likely than another.

**The request radius** is the radius, plus the largest offset that could have
been drawn, plus the hold distance. It does not use the distance that was
drawn, so the size of the request says nothing about the draw.

**The hold.** An offset stays in force until the position is more than the
hold distance from the point where it was drawn. The hold distance is the
hysteresis: a percentage of the radius. A fixed location has a hold of zero
and one offset for good. A tracker is given a minimum hold, because a phone
lying still reports positions tens of metres apart: with a hold shorter than
that drift each report would leave the record and draw again for the same
place. The hold has no part in which alerts are shown. Those are placed
against the true position on every poll, so a long hold cannot widen what a
small radius lets through.

**Lock records.** A fresh draw each time a place is revisited would give the
place away: it must lie where all the rings overlap, and in simulation fifty
visits left under five square kilometres. So each draw is kept as a record of
the point, its hold radius and its offset. When the position leaves the
current record, an earlier record whose radius contains it is taken up again
with the offset it had. A new record is made only where no record reaches.
Records are therefore points along the routes travelled, more than a hold
distance apart, and their number grows with ground covered, not with time.
Remembering can be switched off; the current record is still kept, so a
restart does not cause a new draw.

Radii overlap, so two to four records usually reach a position. The one made
first is taken up. Which record that is can never change afterwards, because
no record made later is older. The nearest would change whenever a later
record happened to lie closer, and the same spot would then be seen through
one more apparent centre. Over a simulated year the rule made no difference
to which records exist or to how many are in force at one place, and it
changed the request a little less often than taking the nearest did.

No Home Assistant imports and no I/O. The caller supplies the day number and
stores what ``as_dict`` returns. Distances are kilometres.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from array import array
from dataclasses import dataclass
from math import asin, atan2, cos, degrees, floor, pi, radians, sin, sqrt
from typing import Any, Final, Protocol

from .nws_zones import EARTH_RADIUS_KM, KM_PER_DEGREE, haversine_km

MILE_KM: Final = 1.609344

# The offset is this share of the radius, and never from a narrower range
# than the floor: 15 to 35 miles. The two meet at a radius of 100 miles.
OFFSET_LOW_PCT: Final = 15.0
OFFSET_HIGH_PCT: Final = 35.0
OFFSET_LOW_FLOOR_KM: Final = 15.0 * MILE_KM
OFFSET_HIGH_FLOOR_KM: Final = OFFSET_LOW_FLOOR_KM * OFFSET_HIGH_PCT / OFFSET_LOW_PCT

# The shortest hold a tracker should be given, whatever its hysteresis: 8 km,
# about 5 miles. It is well clear of the drift of a phone's reported position,
# and in simulation a hold of 1.6 km gave away more of a route than one of
# 8 km. The caller passes it as ``min_hold_km``; a fixed location passes none.
TRACKER_MIN_HOLD_KM: Final = 8.0

# Positions are kept to five decimal places (about a metre) and distances to
# ten metres, rounded when a record is made, so that a record read back from
# storage is the record that was written. The slack lets the position that
# made a record still count as inside it after that rounding. It is added to
# the request radius as well, so the cover stays exact.
SLACK_KM: Final = 0.01

# The most records one store keeps. Each is seven numbers: 48 bytes in
# storage, 56 in memory, so the ceiling is about a megabyte. Reaching it drops
# the records unused for longest, a twentieth of the store at a time.
MAX_RECORDS: Final = 20_000
STORE_VERSION: Final = 1

# Limits on what a stored record may claim. Half the Earth's circumference is
# as far as any distance on it can be.
_MAX_KM: Final = pi * EARTH_RADIUS_KM
_MAX_DAY: Final = 10_000_000
_FIELDS: Final = 7  # lat, lon, hold_km, offset_km, bearing, offset_max_km, day
_LAT, _LON, _HOLD, _OFFSET, _BEARING, _OFFSET_MAX, _DAY = range(_FIELDS)
_Row = tuple[float, float, float, float, float, float, int]

# One record as it is written in a saved file: seven plain numbers in square
# brackets. Used to find the records in a file that no longer reads as a
# whole. Each part matches in one way only, so a search is linear in the
# length of the text however the text is damaged.
_NUMBER: Final = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
_ROW_IN_TEXT: Final = re.compile(
    r"\[\s*(?:" + _NUMBER + r"\s*,\s*){6}" + _NUMBER + r"\s*\]"
)


class RandomSource(Protocol):
    """What the draw needs: ``random()`` in [0, 1)."""

    def random(self) -> float:
        """Return the next value."""
        ...


def _checked(value: float, low: float, high: float, what: str) -> float:
    """``value`` as a float, or ``ValueError`` when it is outside ``low..high``."""
    # Booleans are not numbers here. The comparison refuses NaN and infinity,
    # and is made before any conversion: a very long integer cannot be one.
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not low <= value <= high
    ):
        raise ValueError(
            f"{what} must be a number between {low:g} and {high:g}, not {value!r}"
        )
    return float(value)


def offset_range_km(radius_km: float) -> tuple[float, float]:
    """The smallest and largest offset that may be drawn for a radius."""
    radius = _checked(radius_km, 0.0, _MAX_KM, "radius_km")
    return (
        max(radius * OFFSET_LOW_PCT / 100.0, OFFSET_LOW_FLOOR_KM),
        max(radius * OFFSET_HIGH_PCT / 100.0, OFFSET_HIGH_FLOOR_KM),
    )


def hold_km(radius_km: float, hysteresis_pct: float) -> float:
    """How far the position may move before the request is recalculated."""
    radius = _checked(radius_km, 0.0, _MAX_KM, "radius_km")
    return radius * _checked(hysteresis_pct, 0.0, 100.0, "hysteresis_pct") / 100.0


def draw_offset(radius_km: float, rng: RandomSource) -> tuple[float, float]:
    """A random offset for the radius: distance in km, bearing in degrees.

    The distance is uniform by area between the two limits, so the true
    position is equally likely anywhere in the ring around the apparent
    centre. Rounded to ten metres and a tenth of a degree, which is how a
    record keeps them.
    """
    low, high = offset_range_km(radius_km)
    distance = sqrt(low * low + _unit(rng) * (high * high - low * low))
    bearing = round(_unit(rng) * 360.0, 1) % 360.0
    return min(max(round(distance, 2), round(low, 2)), round(high, 2)), bearing


def _unit(rng: RandomSource) -> float:
    """The next value of the random source, refused unless it is in [0, 1).

    A source that returned anything else would give an offset that is not a
    number, or one outside the range, and the request would be calculated
    from it with nothing to show the fault.
    """
    value = rng.random()
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0.0 <= value < 1.0
    ):
        raise ValueError(
            f"the random source must return a number from 0 up to 1, not {value!r}"
        )
    return float(value)


def displaced(
    lat: float, lon: float, distance_km: float, bearing_deg: float
) -> tuple[float, float]:
    """The point ``distance_km`` from a position along a bearing, on a sphere.

    Longitude comes back in -180 to 180.
    """
    phi, theta = radians(lat), radians(bearing_deg)
    delta = distance_km / EARTH_RADIUS_KM
    sin_phi = sin(phi) * cos(delta) + cos(phi) * sin(delta) * cos(theta)
    phi2 = asin(min(1.0, max(-1.0, sin_phi)))
    lam = radians(lon) + atan2(
        sin(theta) * sin(delta) * cos(phi), cos(delta) - sin(phi) * sin(phi2)
    )
    return degrees(phi2), (degrees(lam) + 180.0) % 360.0 - 180.0


@dataclass(frozen=True, slots=True)
class LockRecord:
    """The record in force: where the request is calculated from, and how wide."""

    lat: float  # the point where the offset was drawn
    lon: float
    hold_km: float  # the record holds while the position is within this
    offset_km: float
    bearing_deg: float
    offset_max_km: float  # the largest offset that could have been drawn

    @property
    def centre(self) -> tuple[float, float]:
        """The apparent centre: the point the zone list is calculated around."""
        return displaced(self.lat, self.lon, self.offset_km, self.bearing_deg)

    def request_radius_km(self, radius_km: float) -> float:
        """The radius to ask about, around ``centre``, to cover ``radius_km``
        around every position this record holds for.

        Built from the largest possible offset, not the one drawn.
        """
        radius = _checked(radius_km, 0.0, _MAX_KM, "radius_km")
        return radius + self.offset_max_km + self.hold_km + SLACK_KM


class LockStore:
    """The lock records of one location or tracker, and the one in force.

    ``lock_for`` is the whole interface during a poll. ``as_dict`` and
    ``from_dict`` carry the store to and from storage, and ``changed`` says
    whether there is anything new to write. One lock guards every method, so
    a call from the executor and one from the event loop cannot interleave.
    """

    def __init__(
        self,
        *,
        remember: bool = True,
        max_records: int = MAX_RECORDS,
        rng: RandomSource | None = None,
    ) -> None:
        """An empty store. ``remember=False`` keeps the current record only."""
        if (
            isinstance(max_records, bool)
            or not isinstance(max_records, int)
            or not 1 <= max_records <= MAX_RECORDS
        ):
            raise ValueError(
                f"max_records must be a whole number between 1 and {MAX_RECORDS}, "
                f"not {max_records!r}"
            )
        self._remember = remember
        self._max_records = max_records
        self._rng: RandomSource = rng if rng is not None else secrets.SystemRandom()
        self._data: array[float] = array("d")
        self._cells: dict[tuple[int, int], list[int]] = {}
        self._max_hold = 0.0
        self._current: int | None = None
        self._guard = threading.Lock()
        self.changed = False

    def __len__(self) -> int:
        """How many records are kept."""
        with self._guard:
            return len(self._data) // _FIELDS

    # -- the poll -----------------------------------------------------------

    def lock_for(
        self,
        lat: float,
        lon: float,
        *,
        radius_km: float,
        hysteresis_pct: float,
        today: int,
        min_hold_km: float = 0.0,
    ) -> LockRecord:
        """The record in force for a position, taking up or making one if needed.

        ``today`` is a day number, any that rises by one a day; it only orders
        records by when they were last used. ``min_hold_km`` is the shortest
        hold a record made now may have: ``TRACKER_MIN_HOLD_KM`` for a
        tracker, nothing for a fixed location. Raises ``ValueError`` for a
        position, radius, percentage, minimum or day that is not a usable
        number.
        """
        lat = _checked(lat, -90.0, 90.0, "lat")
        lon = _checked(lon, -180.0, 180.0, "lon")
        # No longer than the longest hold a record at the smallest offset
        # limit may have, or the record made would be refused when read back.
        longest = OFFSET_HIGH_FLOOR_KM * 100.0 / OFFSET_HIGH_PCT
        hold = max(
            hold_km(radius_km, hysteresis_pct),
            _checked(min_hold_km, 0.0, longest, "min_hold_km"),
        )
        day = int(_checked(today, 0, _MAX_DAY, "today"))
        if day != today:
            raise ValueError(f"today must be a whole number, not {today!r}")
        with self._guard:
            index = self._current
            if index is None or not self._holds(index, lat, lon):
                index = self._find(lat, lon)
                if index is None:
                    index = self._make(lat, lon, radius_km, hold, day)
                self._current = index
                self.changed = True
            self._touch(index, day)
            return self._lock(index)

    def _holds(self, index: int, lat: float, lon: float) -> bool:
        base = index * _FIELDS
        data = self._data
        away = haversine_km(lat, lon, data[base + _LAT], data[base + _LON])
        return away <= data[base + _HOLD] + SLACK_KM

    def _find(self, lat: float, lon: float) -> int | None:
        """The first made of the records whose hold radius contains the position."""
        data = self._data
        reach = self._max_hold + SLACK_KM
        reach_lat = reach / KM_PER_DEGREE
        south, north = floor(lat - reach_lat), floor(lat + reach_lat)
        widest = abs(lat) + reach_lat
        # Near a pole a short distance spans every longitude.
        reach_lon = (
            180.0 if widest >= 89.0 else reach / (KM_PER_DEGREE * cos(radians(widest)))
        )
        if reach_lon >= 180.0:
            columns: list[int] = list(range(-180, 180))
        else:
            west, east = floor(lon - reach_lon), floor(lon + reach_lon)
            columns = sorted({(x + 180) % 360 - 180 for x in range(west, east + 1)})
        first: int | None = None
        for row in range(south, north + 1):
            for column in columns:
                for index in self._cells.get((row, column), ()):
                    if first is not None and index > first:
                        continue  # made later than one already found to reach
                    base = index * _FIELDS
                    away = haversine_km(lat, lon, data[base + _LAT], data[base + _LON])
                    if away <= data[base + _HOLD] + SLACK_KM:
                        first = index
        return first

    def _make(
        self, lat: float, lon: float, radius_km: float, hold: float, day: int
    ) -> int:
        _low, high = offset_range_km(radius_km)
        # Drawn before anything is discarded: if the draw fails, the store is
        # as it was.
        distance, bearing = draw_offset(radius_km, self._rng)
        if not self._remember:
            self._clear()
        point_lat = round(lat, 5)
        point_lon = round(lon, 5)
        if point_lon >= 180.0:  # the cells run from -180 up to, not including, 180
            point_lon -= 360.0
        self._append(
            (
                point_lat,
                point_lon,
                round(hold, 2),
                distance,
                bearing,
                round(high, 2),
                day,
            )
        )
        index = len(self._data) // _FIELDS - 1
        if index + 1 > self._max_records:
            index = self._evict(keep=index)
        return index

    def _append(self, record: _Row) -> None:
        index = len(self._data) // _FIELDS
        self._data.extend(record)
        key = (floor(record[_LAT]), floor(record[_LON]))
        self._cells.setdefault(key, []).append(index)
        self._max_hold = max(self._max_hold, record[_HOLD])

    def _clear(self) -> None:
        self._data = array("d")
        self._cells = {}
        self._max_hold = 0.0
        self._current = None

    def _evict(self, *, keep: int) -> int:
        """Drop the records unused for longest; return ``keep``'s new index."""
        data = self._data
        count = len(data) // _FIELDS
        target = max(1, self._max_records - max(1, self._max_records // 20))
        oldest_first = sorted(range(count), key=lambda i: (data[i * _FIELDS + _DAY], i))
        dropped: set[int] = set()
        for index in oldest_first:
            if count - len(dropped) <= target:
                break
            if index != keep:
                dropped.add(index)
        survivors = [i for i in range(count) if i not in dropped]
        rows = [self._row(i) for i in survivors]
        self._clear()
        for row in rows:
            self._append(row)
        return survivors.index(keep)

    def _row(self, index: int) -> _Row:
        base = index * _FIELDS
        data = self._data
        return (
            data[base + _LAT],
            data[base + _LON],
            data[base + _HOLD],
            data[base + _OFFSET],
            data[base + _BEARING],
            data[base + _OFFSET_MAX],
            int(data[base + _DAY]),
        )

    def _touch(self, index: int, day: int) -> None:
        position = index * _FIELDS + _DAY
        if self._data[position] != day:
            self._data[position] = day
            self.changed = True

    def _lock(self, index: int) -> LockRecord:
        row = self._data[index * _FIELDS : (index + 1) * _FIELDS]
        return LockRecord(
            row[_LAT],
            row[_LON],
            row[_HOLD],
            row[_OFFSET],
            row[_BEARING],
            row[_OFFSET_MAX],
        )

    # -- storage ------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """The store as plain JSON data. Marks it as written."""
        with self._guard:
            data = self._data
            records = [
                [*data[base : base + _DAY], int(data[base + _DAY])]
                for base in range(0, len(data), _FIELDS)
            ]
            self.changed = False
            return {
                "version": STORE_VERSION,
                "current": self._current,
                "records": records,
            }

    @classmethod
    def from_dict(
        cls,
        stored: Any,
        *,
        remember: bool = True,
        max_records: int = MAX_RECORDS,
        rng: RandomSource | None = None,
    ) -> tuple[LockStore, str | None]:
        """A store from what ``as_dict`` returned, and a report of anything refused.

        The data comes from a file, so it is taken to be anything at all, and
        nothing in it makes this raise. A record that could not have been
        made by this module is left out and the rest are kept, since each
        record kept is one place that will not be drawn again. The second
        value is ``None`` when everything was accepted, and otherwise says
        what was not; the caller logs it, so that a damaged file is reported
        and not passed over.
        """
        store = cls(remember=remember, max_records=max_records, rng=rng)
        if stored is None:
            return store, None
        version = stored.get("version") if isinstance(stored, dict) else None
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != STORE_VERSION
        ):
            return (
                store,
                "not a lock record store of a version this reads; starting empty",
            )
        rows = stored.get("records")
        if not isinstance(rows, list):
            return store, "it holds no list of records; starting empty"
        refused = 0
        kept_at: dict[int, int] = {}
        for position, row in enumerate(rows[: store._max_records]):
            record = _record(row)
            if record is None:
                refused += 1
                continue
            kept_at[position] = len(store._data) // _FIELDS
            store._append(record)
        over = max(0, len(rows) - store._max_records)
        current = stored.get("current")
        lost_current = False
        if (
            isinstance(current, int)
            and not isinstance(current, bool)
            and current in kept_at
        ):
            store._current = kept_at[current]
        elif current is not None:
            lost_current = True
        if not remember and len(store._data) > (_FIELDS if store._current == 0 else 0):
            # Remembering was switched off since this was written: keep the
            # record in force and nothing else.
            in_force = None if store._current is None else store._row(store._current)
            store._clear()
            if in_force is not None:
                store._append(in_force)
                store._current = 0
            store.changed = True
        problems = []
        if refused:
            problems.append(f"{refused} unreadable record(s) left out")
        if over:
            problems.append(
                f"{over} record(s) beyond the limit of {store._max_records} left out"
            )
        if lost_current:
            problems.append(
                "the record in force could not be read, so one will be taken up or drawn"
            )
        if problems:
            store.changed = True
            return store, "; ".join(problems)
        return store, None

    @classmethod
    def salvage(
        cls,
        text: Any,
        *,
        remember: bool = True,
        max_records: int = MAX_RECORDS,
        rng: RandomSource | None = None,
    ) -> tuple[LockStore, str]:
        """A store from the text of a saved file that no longer reads as one.

        For a file that was cut short, overwritten in part, or damaged so
        that it is no longer the document ``as_dict`` returned. Every record
        still written whole in the text is found and put through the checks
        ``from_dict`` makes, in the order of the text, which is the order
        the records were made in. Nothing in the text makes this raise. No
        record is in force afterwards: which one was cannot be told from
        the pieces, and the next position takes up the first made that
        reaches it, as it would have. The second value says how many
        records were recovered, for the log.
        """
        rows: list[Any] = []
        if isinstance(text, str):
            for found in _ROW_IN_TEXT.finditer(text):
                try:
                    rows.append(json.loads(found.group()))
                except (ValueError, RecursionError):
                    continue  # a number too long to read
        store, problem = cls.from_dict(
            {"version": STORE_VERSION, "current": None, "records": rows},
            remember=remember,
            max_records=max_records,
            rng=rng,
        )
        store.changed = True
        report = (
            "the file does not read as it was written; "
            f"{len(store)} record(s) recovered from it"
        )
        return store, f"{report}; {problem}" if problem else report


def _record(row: Any) -> _Row | None:
    """One stored record, or ``None`` when it is not one that could have been made.

    Seven numbers within their limits is not enough. A record whose offset is
    smaller than any that could have been drawn is refused: a damaged file of
    zeros reads as a record with no offset, and used as it stands it would
    put the request on the true position, with no error to show for it. A
    hold longer than the radius the record's offset limit belongs to is
    refused for the same reason: it would keep one record in force over any
    distance.
    """
    if not isinstance(row, list) or len(row) != _FIELDS:
        return None
    for value in row:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
    lat, lon, hold, offset, bearing, offset_max, day = row
    # Each comparison is false for NaN and for the infinities, and none
    # converts its value, so an integer too long for a float is refused too.
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    if not (0.0 <= hold <= _MAX_KM and 0.0 <= offset <= offset_max <= _MAX_KM):
        return None
    if not (0.0 <= bearing <= 360.0 and 0 <= day <= _MAX_DAY and day == int(day)):
        return None
    if offset_max < round(OFFSET_HIGH_FLOOR_KM, 2):
        return None
    # The smallest offset is a fixed share of the largest at every radius, and
    # the radius is the largest offset over that share. The margins are the
    # rounding of the stored figures.
    if offset < round(offset_max * OFFSET_LOW_PCT / OFFSET_HIGH_PCT, 2) - 0.01:
        return None
    if hold > offset_max * 100.0 / OFFSET_HIGH_PCT + 0.05:
        return None
    return (
        float(lat),
        -180.0 if lon == 180.0 else float(lon),
        float(hold),
        float(offset),
        float(bearing),
        float(offset_max),
        int(day),
    )
