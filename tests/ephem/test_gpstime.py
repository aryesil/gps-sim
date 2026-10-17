import datetime as dt

import pytest

from backend.gpstime import (
    GPSTime, WEEK_SECONDS, gps_utc_offset, utc_to_gps, gps_to_utc,
)


def test_gps_epoch_is_week_zero():
    g = GPSTime.from_datetime(dt.datetime(1980, 1, 6, 0, 0, 0))
    assert g.week == 0
    assert g.sow == pytest.approx(0.0, abs=1e-6)


def test_known_epoch_2026_08_28_toe():
    # brdc_sample.rnx fixture day: GPS week 2433, TOW 475200 s.
    g = utc_to_gps(dt.datetime(2026, 8, 28, 11, 59, 42))
    assert g.week == 2433
    assert g.sow == pytest.approx(475200.0, abs=1e-6)


def test_leap_second_table_boundaries():
    assert gps_utc_offset(dt.datetime(2015, 6, 30, 23, 59, 59)) == 16
    assert gps_utc_offset(dt.datetime(2015, 7, 1, 0, 0, 0)) == 17
    assert gps_utc_offset(dt.datetime(2017, 1, 1, 0, 0, 0)) == 18
    assert gps_utc_offset(dt.datetime(2030, 1, 1)) == 18   # latest known


def test_utc_gps_roundtrip_applies_18s_now():
    when = dt.datetime(2026, 8, 28, 12, 0, 0)
    g = utc_to_gps(when)
    # 18 s ahead of UTC on the GPS timescale
    assert g.to_gps_datetime() - when == dt.timedelta(seconds=18)
    assert g.to_datetime() == when


def test_gps_to_utc_helper_matches():
    g = utc_to_gps(dt.datetime(2020, 3, 1, 6, 30, 0))
    assert gps_to_utc(g.week, g.sow) == dt.datetime(2020, 3, 1, 6, 30, 0)


def test_sow_normalises_and_carries_week():
    g = GPSTime(2433, WEEK_SECONDS + 10.0)
    assert g.week == 2434
    assert g.sow == pytest.approx(10.0)
    g2 = GPSTime(2433, -5.0)
    assert g2.week == 2432
    assert g2.sow == pytest.approx(WEEK_SECONDS - 5.0)


def test_week_rollover_arithmetic_is_continuous():
    end = GPSTime(2433, WEEK_SECONDS - 30.0)
    start = GPSTime(2433, WEEK_SECONDS - 90.0)
    assert (end - start) == pytest.approx(60.0)
    later = end.shifted(120.0)   # crosses the week boundary
    assert later.week == 2434
    assert (later - end) == pytest.approx(120.0)


def test_from_seconds_matches_seconds_property():
    g = GPSTime(2433, 123456.0)
    assert GPSTime.from_seconds(g.seconds).week == 2433
    assert GPSTime.from_seconds(g.seconds).sow == pytest.approx(123456.0)


def test_from_gps_datetime_has_no_leap_step():
    when = dt.datetime(2026, 8, 28, 12, 0, 0)
    g_gps = GPSTime.from_gps_datetime(when)
    g_utc = GPSTime.from_datetime(when)
    assert (g_gps - g_utc) == pytest.approx(-18.0)


def test_day_of_week():
    # 2026-08-28 is a Friday -> GPS day-of-week 5 (Sun=0).
    g = utc_to_gps(dt.datetime(2026, 8, 28, 12, 0, 0))
    assert g.day_of_week == 5
