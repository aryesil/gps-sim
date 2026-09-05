"""Scenarios that straddle a GPS-week rollover or a UTC midnight must
stay continuous: GPS time keeps advancing monotonically, the
week/sow representation wraps cleanly, and satellite geometry has no
step at the boundary.
"""
import datetime as dt
import pathlib

import numpy as np
import pytest

from backend import ephemeris, geometry, gpstime

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "brdc_sample.rnx"
RX = geometry.llh_to_ecef(41.0082, 28.9784, 100.0)


@pytest.fixture(scope="module")
def eph():
    return ephemeris.parse_rinex(FIX)


def _week_rollover_utc():
    # a GPS week starts at Sunday 00:00:00 GPS time; find one near the fixture
    g = gpstime.GPSTime(2338, 604790.0)          # 10 s before rollover
    return g.to_datetime()


def test_gps_seconds_are_monotonic_across_week_rollover():
    start = gpstime.GPSTime(2338, 604790.0)
    ts = [start.shifted(s) for s in np.arange(0, 20, 0.5)]
    secs = [t.seconds for t in ts]
    assert all(b > a for a, b in zip(secs, secs[1:]))
    # the representation wrapped: week advanced, sow fell back near 0
    assert ts[0].week == 2338 and ts[-1].week == 2339
    assert ts[-1].sow < 15.0


def test_sow_wraps_without_gap():
    a = gpstime.GPSTime(2338, 604799.0)
    b = a.shifted(2.0)
    assert b.week == 2339
    assert b.sow == pytest.approx(1.0, abs=1e-6)
    assert b - a == pytest.approx(2.0, abs=1e-9)


def test_utc_gps_roundtrip_across_utc_midnight():
    base = dt.datetime(2026, 3, 1, 23, 59, 55)
    for k in range(11):
        u = base + dt.timedelta(seconds=k)
        g = gpstime.utc_to_gps(u)
        back = gpstime.gps_to_utc(g.week, g.sow)
        assert abs((back - u).total_seconds()) < 1e-6


def test_satellite_geometry_is_continuous_across_week_rollover(eph):
    start = gpstime.GPSTime(2338, 604790.0)
    prn, e = next(iter(eph.items()))
    # geometry wants a SoW consistent with the ephemeris toe (same week);
    # the +/- half-week unwrap in sat_state absorbs the rollover.
    rng = []
    for s in np.arange(0.0, 20.0, 0.5):
        t = start.shifted(s)
        rng.append(geometry.observables(e, RX, t.sow)["geo_range_m"])
    d = np.abs(np.diff(rng))
    assert d.max() < 8.0 * (np.median(d) or 1e-9)   # no step at the boundary


def test_doppler_is_continuous_across_week_rollover(eph):
    start = gpstime.GPSTime(2338, 604790.0)
    prn, e = next(iter(eph.items()))
    fd = []
    for s in np.arange(0.0, 20.0, 0.5):
        t = start.shifted(s)
        fd.append(geometry.observables(e, RX, t.sow)["carrier_doppler_hz"])
    d = np.abs(np.diff(fd))
    assert d.max() < 8.0 * (np.median(d) or 1e-9)
