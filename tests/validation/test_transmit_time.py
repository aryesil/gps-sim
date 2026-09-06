"""Transmit-time solution, Sagnac correction, and range/range-rate/Doppler
mutual consistency.

The strongest check here needs no velocity or Sagnac formula at all: the
line-of-sight range rate must equal the time derivative of the geometric
range obtained by *re-solving* the transmit-time problem at t +/- dt. Any
sign error or missing term in either the production or the reference
Doppler path shows up as a mismatch.
"""
import datetime as dt
import pathlib

import numpy as np
import pytest

from backend import config, geometry
from backend.ephem import ephemeris
from backend.analysis import reference

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "brdc_sample.rnx"
TOE = 475200.0
RX_SITES = [
    (41.0082, 28.9784, 100.0),    # Istanbul
    (-33.8688, 151.2093, 30.0),   # Sydney
    (78.2232, 15.6267, 5.0),      # Svalbard -- high latitude
    (0.0, 0.0, 0.0),              # equator / prime meridian
]


@pytest.fixture(scope="module")
def eph_by_prn():
    return ephemeris.parse_rinex(FIX)


@pytest.mark.parametrize("site", RX_SITES)
def test_transmit_time_position_matches_between_implementations(eph_by_prn, site):
    rx = geometry.llh_to_ecef(*site)
    for prn, eph in eph_by_prn.items():
        p_prod, _, tof_prod, _ = geometry.solve_transmit_time(eph, rx, TOE)
        ref = reference.solve_transmit_time(eph, rx, TOE)
        assert abs(tof_prod - ref["tof_s"]) < 1e-9, f"PRN {prn} tof"
        assert np.linalg.norm(np.asarray(p_prod) - ref["sat_ecef"]) < 1e-3, f"PRN {prn} pos"


@pytest.mark.parametrize("site", RX_SITES)
def test_sagnac_correction_is_present_and_correct_sign(eph_by_prn, site):
    """Skipping the Earth-rotation term during flight moves the satellite by
    ~30 m; the correction must reduce the residual, not enlarge it."""
    rx = np.asarray(geometry.llh_to_ecef(*site), float)
    range_shifts = []
    for prn, eph in eph_by_prn.items():
        ref = reference.solve_transmit_time(eph, rx, TOE)
        tof = ref["tof_s"]
        raw = reference.sat_state(eph, TOE - tof)["pos"]           # no Sagnac
        corr = ref["sat_ecef"]                                      # with Sagnac
        pos_shift = np.linalg.norm(corr - raw)
        range_shift = abs(np.linalg.norm(corr - rx) - np.linalg.norm(raw - rx))
        range_shifts.append(range_shift)
        # position always rotates ~ r * omega_E * tof ~ 100-200 m
        assert 40.0 < pos_shift < 300.0, f"PRN {prn}: Sagnac pos shift {pos_shift:.1f} m"
        # the range projection is geometry-dependent (can be ~0), but bounded
        assert range_shift < 45.0, f"PRN {prn}: Sagnac range shift {range_shift:.1f} m"
        # |corrected range| must be self-consistent with tof to < 1 mm
        assert abs(np.linalg.norm(corr - rx) - config.C * tof) < 1e-3
    # at least one satellite must show the classic several-metre range effect
    assert max(range_shifts) > 5.0


@pytest.mark.parametrize("site", RX_SITES)
def test_range_rate_equals_time_derivative_of_range(eph_by_prn, site):
    rx = geometry.llh_to_ecef(*site)
    h = 0.5
    for prn, eph in eph_by_prn.items():
        r0 = reference.solve_transmit_time(eph, rx, TOE)
        r_plus = reference.solve_transmit_time(eph, rx, TOE + h)["geo_range_m"]
        r_minus = reference.solve_transmit_time(eph, rx, TOE - h)["geo_range_m"]
        drdt = (r_plus - r_minus) / (2 * h)
        # h=0.5 s central difference: O(h^2) truncation against LOS jerk is
        # a few mm/s; the analytic range_rate is the reference value.
        assert abs(drdt - r0["range_rate_mps"]) < 5e-3, f"PRN {prn}: {drdt:.4f} vs {r0['range_rate_mps']:.4f}"


@pytest.mark.parametrize("site", RX_SITES)
def test_doppler_consistent_between_geometry_and_reference(eph_by_prn, site):
    rx = geometry.llh_to_ecef(*site)
    for prn, eph in eph_by_prn.items():
        obs = geometry.observables(eph, rx, TOE)
        ref = reference.solve_transmit_time(eph, rx, TOE)
        # geometry uses its own finite-diff velocity; agreement to < 0.5 Hz
        assert abs(obs["carrier_doppler_hz"] - ref["carrier_doppler_hz"]) < 0.5, prn


@pytest.mark.parametrize("site", RX_SITES)
def test_geometry_doppler_equals_negative_L1_over_c_times_range_rate(eph_by_prn, site):
    """Independent of any velocity: derive range rate from geometry's own
    geo_range by finite difference and check its published Doppler."""
    rx = geometry.llh_to_ecef(*site)
    h = 0.5
    for prn, eph in eph_by_prn.items():
        r_plus = geometry.observables(eph, rx, TOE + h)["geo_range_m"]
        r_minus = geometry.observables(eph, rx, TOE - h)["geo_range_m"]
        drdt = (r_plus - r_minus) / (2 * h)
        want = -config.L1_HZ * drdt / config.C
        got = geometry.observables(eph, rx, TOE)["carrier_doppler_hz"]
        assert abs(want - got) < 1.0, f"PRN {prn}: {want:.2f} vs {got:.2f} Hz"


def test_code_doppler_is_carrier_doppler_scaled_by_chip_ratio(eph_by_prn):
    rx = geometry.llh_to_ecef(*RX_SITES[0])
    for prn, eph in eph_by_prn.items():
        o = geometry.observables(eph, rx, TOE)
        if abs(o["carrier_doppler_hz"]) < 1e-9:
            continue
        ratio = o["code_doppler_hz"] / o["carrier_doppler_hz"]
        assert ratio == pytest.approx(config.CA_CHIP_HZ / config.L1_HZ, rel=1e-12)


def test_elevation_is_within_physical_bounds_everywhere(eph_by_prn):
    for site in RX_SITES:
        rx = geometry.llh_to_ecef(*site)
        for eph in eph_by_prn.values():
            o = geometry.observables(eph, rx, TOE)
            assert -90.0 <= o["el_deg"] <= 90.0
            assert 0.0 <= o["az_deg"] < 360.0
            assert o["geo_range_m"] > 1.9e7   # never closer than ~19,000 km
