"""Atmosphere layer wired into geometry.observables: off == legacy, on ==
one extra positive pseudorange delay, geometry untouched.
"""
import math
import pathlib

import numpy as np
import pytest

from backend import atmosphere, ephemeris, geometry

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "brdc_sample.rnx"
TOE = 475200.0
SITE = (41.0082, 28.9784, 100.0)


@pytest.fixture(scope="module")
def eph_by_prn():
    return ephemeris.parse_rinex(FIX)


def _atmo_fn(cfg, sow):
    lat, lon, h = math.radians(SITE[0]), math.radians(SITE[1]), SITE[2]
    return lambda az, el: atmosphere.delays_for_los(cfg, sow, lat, lon, h, az, el)["total_m"]


def test_no_atmo_fn_reproduces_legacy_observables(eph_by_prn):
    rx = geometry.llh_to_ecef(*SITE)
    for prn, eph in eph_by_prn.items():
        legacy = geometry.observables(eph, rx, TOE)
        withnone = geometry.observables(eph, rx, TOE, atmo_delay_fn=None)
        assert legacy["pseudorange_m"] == withnone["pseudorange_m"]
        assert withnone["atmo_delay_m"] == 0.0


def test_off_config_changes_nothing(eph_by_prn):
    rx = geometry.llh_to_ecef(*SITE)
    fn = _atmo_fn(atmosphere.AtmosphereConfig(), TOE)
    for prn, eph in eph_by_prn.items():
        base = geometry.observables(eph, rx, TOE)
        got = geometry.observables(eph, rx, TOE, atmo_delay_fn=fn)
        assert got["pseudorange_m"] == pytest.approx(base["pseudorange_m"], abs=1e-9)


def test_enabled_adds_positive_delay_only_to_pseudorange(eph_by_prn):
    rx = geometry.llh_to_ecef(*SITE)
    cfg = atmosphere.AtmosphereConfig(ionosphere="klobuchar", troposphere="saastamoinen")
    fn = _atmo_fn(cfg, TOE)
    for prn, eph in eph_by_prn.items():
        base = geometry.observables(eph, rx, TOE)
        got = geometry.observables(eph, rx, TOE, atmo_delay_fn=fn)
        d = got["pseudorange_m"] - base["pseudorange_m"]
        assert d > 0.0
        assert d == pytest.approx(got["atmo_delay_m"], rel=1e-9)
        # geometric range and Doppler are unchanged
        assert got["geo_range_m"] == base["geo_range_m"]
        assert got["carrier_doppler_hz"] == base["carrier_doppler_hz"]
        # code phase moved by exactly the delay in chips
        dchips = (d / geometry.config.C * geometry.config.CA_CHIP_HZ)
        assert ((got["code_phase_chips"] - base["code_phase_chips"] - dchips + 511.5)
                % 1023 - 511.5) == pytest.approx(0.0, abs=1e-6)


def test_delay_is_larger_at_low_elevation(eph_by_prn):
    rx = geometry.llh_to_ecef(*SITE)
    cfg = atmosphere.AtmosphereConfig(ionosphere="klobuchar", troposphere="saastamoinen")
    fn = _atmo_fn(cfg, TOE)
    rows = []
    for prn, eph in eph_by_prn.items():
        o = geometry.observables(eph, rx, TOE, atmo_delay_fn=fn)
        rows.append((o["el_deg"], o["atmo_delay_m"]))
    rows.sort()
    # lowest-elevation sat has the largest delay
    assert rows[0][1] == max(r[1] for r in rows)


def test_applied_once_not_twice(eph_by_prn):
    """Calling observables twice with the same fn must not accumulate."""
    rx = geometry.llh_to_ecef(*SITE)
    cfg = atmosphere.AtmosphereConfig(troposphere="saastamoinen")
    fn = _atmo_fn(cfg, TOE)
    eph = next(iter(eph_by_prn.values()))
    a = geometry.observables(eph, rx, TOE, atmo_delay_fn=fn)["pseudorange_m"]
    b = geometry.observables(eph, rx, TOE, atmo_delay_fn=fn)["pseudorange_m"]
    assert a == b
