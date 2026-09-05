# backend/gpstime.py
"""GPS time as a first-class value.

The rest of the codebase carries GPS time as loose floats (seconds of week,
or "GPS TOW") and applies the GPS-UTC offset ad hoc -- see
``ephemeris._gps_seconds`` and ``app._gps_tow``. That is fine for the
broadcast path, where every satellite's ``toe`` is realigned to the request
anyway, but the precise-ephemeris subsystem (backend/precise.py) needs
unambiguous week / seconds-of-week arithmetic that is safe across a week
rollover and across the GPS epoch, plus a real leap-second table.

This module is additive. It does not change the broadcast helpers; it is the
single time authority for the precise path.

Conventions
-----------
* "UTC datetime" means a naive ``datetime`` understood as UTC (the same
  convention the existing code uses -- see ``ephemeris._to_datetime``).
* GPS time is continuous (no leap seconds). GPS-UTC is a positive, integer,
  monotonically non-decreasing number of seconds.
* ``GPSTime`` normalises so ``0 <= sow < 604800`` and ``week`` absorbs the
  overflow.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

GPS_EPOCH = dt.datetime(1980, 1, 6, 0, 0, 0)
WEEK_SECONDS = 604800.0
DAY_SECONDS = 86400.0

# (UTC instant at which the offset below became effective, GPS-UTC seconds).
# Source: IERS Bulletin C history. Extend when the next leap second is
# announced. Entries must be sorted by date.
_LEAP_TABLE: list[tuple[dt.datetime, int]] = [
    (dt.datetime(1980, 1, 6), 0),
    (dt.datetime(1981, 7, 1), 1),
    (dt.datetime(1982, 7, 1), 2),
    (dt.datetime(1983, 7, 1), 3),
    (dt.datetime(1985, 7, 1), 4),
    (dt.datetime(1988, 1, 1), 5),
    (dt.datetime(1990, 1, 1), 6),
    (dt.datetime(1991, 1, 1), 7),
    (dt.datetime(1992, 7, 1), 8),
    (dt.datetime(1993, 7, 1), 9),
    (dt.datetime(1994, 7, 1), 10),
    (dt.datetime(1996, 1, 1), 11),
    (dt.datetime(1997, 7, 1), 12),
    (dt.datetime(1999, 1, 1), 13),
    (dt.datetime(2006, 1, 1), 14),
    (dt.datetime(2009, 1, 1), 15),
    (dt.datetime(2012, 7, 1), 16),
    (dt.datetime(2015, 7, 1), 17),
    (dt.datetime(2017, 1, 1), 18),
]


def gps_utc_offset(when_utc: dt.datetime) -> int:
    """GPS-UTC (seconds) in effect at ``when_utc``.

    For instants before the first table entry the earliest offset is
    returned; the table is authoritative for every GPS-era date this
    project can encounter.
    """
    off = _LEAP_TABLE[0][1]
    for date, value in _LEAP_TABLE:
        if when_utc >= date:
            off = value
        else:
            break
    return off


@dataclass(frozen=True)
class GPSTime:
    """A GPS-timescale instant, normalised to ``0 <= sow < WEEK_SECONDS``."""

    week: int
    sow: float

    def __post_init__(self) -> None:
        w, s = self.week, float(self.sow)
        carry = int(s // WEEK_SECONDS)
        s -= carry * WEEK_SECONDS
        # frozen dataclass: assign through object.__setattr__
        object.__setattr__(self, "week", int(w) + carry)
        object.__setattr__(self, "sow", s)

    # --- constructors --------------------------------------------------
    @classmethod
    def from_seconds(cls, seconds_since_gps_epoch: float) -> "GPSTime":
        w = int(seconds_since_gps_epoch // WEEK_SECONDS)
        return cls(w, seconds_since_gps_epoch - w * WEEK_SECONDS)

    @classmethod
    def from_datetime(cls, when_utc: dt.datetime) -> "GPSTime":
        """UTC datetime -> GPS time (leap seconds applied)."""
        gps = (when_utc - GPS_EPOCH).total_seconds() + gps_utc_offset(when_utc)
        return cls.from_seconds(gps)

    @classmethod
    def from_gps_datetime(cls, when_gps: dt.datetime) -> "GPSTime":
        """A datetime already on the GPS timescale (no leap-second step)."""
        return cls.from_seconds((when_gps - GPS_EPOCH).total_seconds())

    # --- accessors ---------------------------------------------------
    @property
    def seconds(self) -> float:
        """Continuous seconds since the GPS epoch."""
        return self.week * WEEK_SECONDS + self.sow

    @property
    def day_of_week(self) -> int:
        """0 = Sunday .. 6 = Saturday (GPS convention)."""
        return int(self.sow // DAY_SECONDS)

    def to_gps_datetime(self) -> dt.datetime:
        return GPS_EPOCH + dt.timedelta(seconds=self.seconds)

    def to_datetime(self) -> dt.datetime:
        """GPS time -> UTC datetime (leap seconds removed).

        The offset is resolved iteratively: the leap count depends on the
        UTC instant, which is what we are solving for. Two passes converge
        for every real leap-second history (the step is at most 1 s).
        """
        approx = self.to_gps_datetime()
        for _ in range(3):
            off = gps_utc_offset(approx)
            nxt = self.to_gps_datetime() - dt.timedelta(seconds=off)
            if nxt == approx:
                break
            approx = nxt
        return approx

    # --- arithmetic ------------------------------------------------
    def shifted(self, seconds: float) -> "GPSTime":
        return GPSTime.from_seconds(self.seconds + seconds)

    def __sub__(self, other: "GPSTime") -> float:
        return self.seconds - other.seconds


def utc_to_gps(when_utc: dt.datetime) -> GPSTime:
    return GPSTime.from_datetime(when_utc)


def gps_to_utc(week: int, sow: float) -> dt.datetime:
    return GPSTime(week, sow).to_datetime()
