"""Canonical scenario truth model: single conversion path, and it
agrees with the independent IS-GPS-200 reference on the inputs it
derives."""
import datetime as dt
import pathlib

import numpy as np
import pytest

from backend import ephemeris, geometry, gpstime, reference, scenario, truth

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "brdc_sample.rnx"


@pytest.fixture(scope="module")
def eph():
    return ephemeris.parse_rinex(FIX)


def _truth():
    return truth.ScenarioTruth(41.0082, 28.9784, 100.0,
                               dt.datetime(2026, 2, 10, 6, 0, 0), 30.0)


def test_conversions_match_the_primitives():
    t = _truth()
    assert t.rx_ecef == geometry.llh_to_ecef(41.0082, 28.9784, 100.0)
    g = gpstime.utc_to_gps(dt.datetime(2026, 2, 10, 6, 0, 0))
    assert (t.gps_week, t.start_sow) == (g.week, g.sow)


def test_from_request_round_trips():
    req = scenario.ScenarioRequest(rinex_path=str(FIX), lat=41.0, lon=29.0,
                                   alt=50.0, start=dt.datetime(2026, 2, 10, 6, 0, 0),
                                   duration_s=10)
    t = truth.from_request(req)
    assert t.rx_ecef == geometry.llh_to_ecef(41.0, 29.0, 50.0)
    assert t.as_dict()["gps_week"] == t.gps_week


def test_sow_at_is_continuous_over_the_run():
    t = _truth()
    offs = np.arange(0, t.duration_s, 1.0)
    secs = [t.start_gps.shifted(o).seconds for o in offs]
    assert all(b > a for a, b in zip(secs, secs[1:]))


def test_observables_agree_with_independent_reference(eph):
    t = _truth()
    rx = np.array(t.rx_ecef)
    sow = t.sow_at(5.0)
    obs = {o["prn"]: o for o in t.observables(eph, offset_s=5.0)}
    assert len(obs) >= 4
    for prn, o in obs.items():
        ref = reference.solve_transmit_time(eph[prn], rx, sow)
        assert o["geo_range_m"] == pytest.approx(ref["geo_range_m"], abs=1e-3)
        assert o["carrier_doppler_hz"] == pytest.approx(ref["carrier_doppler_hz"], abs=0.5)


def test_sat_positions_match_transmit_time_solution(eph):
    t = _truth()
    rx = np.array(t.rx_ecef)
    pos = t.sat_positions(eph, offset_s=0.0)
    for prn, p in pos.items():
        ref = reference.solve_transmit_time(eph[prn], rx, t.start_sow)
        assert np.linalg.norm(np.array(p) - ref["sat_ecef"]) < 1e-3
