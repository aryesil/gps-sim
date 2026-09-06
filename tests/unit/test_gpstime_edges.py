"""GPS-time edge cases and cross-module epoch identity.

The production code carries GPS time two ways: ``backend.gpstime.GPSTime``
(week + seconds-of-week, real leap table) on the precise path, and the loose
``backend.ephem.ephemeris.gps_week_and_sow`` float helper on the broadcast path.
These tests pin the boundary behaviour of the first and prove the two agree
on the same physical epoch, so a scenario epoch cannot silently become two
different GPS epochs in two different modules.
"""
import datetime as dt

import pytest

from backend.ephem import ephemeris
from backend.gpstime import (GPS_EPOCH, WEEK_SECONDS, GPSTime, gps_to_utc,
                             gps_utc_offset, utc_to_gps)


# ---- GPSTime normalisation --------------------------------------------------

def test_sow_at_week_end_rolls_into_next_week():
    t = GPSTime(2000, WEEK_SECONDS)
    assert (t.week, t.sow) == (2001, 0.0)


def test_sow_just_below_week_end_is_untouched():
    t = GPSTime(2000, WEEK_SECONDS - 1e-6)
    assert t.week == 2000
    assert t.sow == pytest.approx(WEEK_SECONDS - 1e-6)


def test_negative_sow_borrows_from_previous_week():
    t = GPSTime(2000, -1.0)
    assert t.week == 1999
    assert t.sow == pytest.approx(WEEK_SECONDS - 1.0)


def test_multi_week_overflow_normalises():
    t = GPSTime(1000, 3 * WEEK_SECONDS + 42.0)
    assert t.week == 1003
    assert t.sow == pytest.approx(42.0)


def test_tow_zero_is_week_boundary_midnight_saturday_sunday():
    t = GPSTime(2200, 0.0)
    assert t.day_of_week == 0
    assert t.to_gps_datetime() == GPS_EPOCH + dt.timedelta(weeks=2200)


def test_seconds_round_trip_through_from_seconds():
    for s in (0.0, 1.0, WEEK_SECONDS - 0.5, 12345678.9, 2200 * WEEK_SECONDS + 7.0):
        t = GPSTime.from_seconds(s)
        assert t.seconds == pytest.approx(s, abs=1e-6)


# ---- leap-second table ----------------------------------------------------

def test_leap_offset_steps_at_2017_boundary():
    assert gps_utc_offset(dt.datetime(2016, 12, 31, 23, 59, 59)) == 17
    assert gps_utc_offset(dt.datetime(2017, 1, 1, 0, 0, 0)) == 18


def test_leap_offset_before_table_clamps_to_earliest():
    assert gps_utc_offset(dt.datetime(1979, 1, 1)) == 0


def test_utc_gps_round_trip_across_leap_boundary():
    for u in (dt.datetime(2016, 12, 31, 23, 59, 58),
              dt.datetime(2017, 1, 1, 0, 0, 1),
              dt.datetime(2026, 9, 4, 0, 0, 0)):
        assert gps_to_utc(*(_ws(utc_to_gps(u)))) == u


def test_gps_minus_utc_is_18_seconds_in_2026():
    g = utc_to_gps(dt.datetime(2026, 9, 4, 12, 0, 0))
    # GPS clock reads 18 s ahead of UTC wall time for the same instant.
    assert (g.to_gps_datetime() - dt.datetime(2026, 9, 4, 12, 0, 0)).total_seconds() == 18.0


# ---- boundaries that have bitten GNSS code before -----------------------

def test_utc_midnight_maps_to_exact_second():
    g = utc_to_gps(dt.datetime(2026, 1, 1, 0, 0, 0))
    assert g.sow == pytest.approx(round(g.sow), abs=1e-9)


def test_week_rollover_is_continuous_in_seconds():
    before = GPSTime(2199, WEEK_SECONDS - 0.5)
    after = before.shifted(1.0)
    assert (after.week, round(after.sow, 6)) == (2200, 0.5)
    assert after.seconds - before.seconds == pytest.approx(1.0)


def test_year_boundary_2025_2026_has_no_leap_step():
    assert gps_utc_offset(dt.datetime(2025, 12, 31)) == gps_utc_offset(dt.datetime(2026, 1, 1))


# ---- cross-module identity ---------------------------------------------

@pytest.mark.parametrize("when_gps", [
    dt.datetime(2026, 8, 28, 12, 0, 0),
    dt.datetime(2026, 1, 1, 0, 0, 0),
    dt.datetime(2019, 4, 7, 0, 0, 0),      # the last GPS week-number rollover
    dt.datetime(2026, 8, 30, 23, 59, 59),  # near a week boundary
])
def test_ephemeris_helper_and_gpstime_agree_on_the_same_epoch(when_gps):
    """``ephemeris.gps_week_and_sow`` and ``GPSTime.from_gps_datetime`` both
    take a GPS-timescale datetime; they must return the same (week, sow)."""
    week_e, sow_e = ephemeris.gps_week_and_sow(when_gps)
    g = GPSTime.from_gps_datetime(when_gps)
    assert week_e == g.week
    assert sow_e == pytest.approx(g.sow, abs=1e-6)


def _ws(g: GPSTime):
    return g.week, g.sow
