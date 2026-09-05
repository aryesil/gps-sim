"""Cross-check the production Kepler propagator against an independent one.

``backend.geometry.sat_state`` feeds the generator, the preview, and the
receiver. ``backend.reference.sat_state`` is a second implementation that
imports none of geometry's helpers (different anomaly solver, analytic
velocity, rotation-matrix ECEF). Agreement between them is evidence that a
bug in one would be caught, not mirrored.

Tolerances are explicit and tight: the two models use identical physical
constants, so the only differences are numerical (iteration scheme,
finite-difference vs analytic velocity).
"""
import pathlib

import numpy as np
import pytest

from backend import ephemeris, geometry, reference

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "brdc_sample.rnx"
TOE = 475200.0

POS_TOL_M = 1e-3           # sub-mm: same physics, different code
VEL_ANALYTIC_TOL = 5e-4   # m/s, analytic (reference) vs 0.5 s central diff (geometry)
VEL_FD_TOL = 1e-5         # m/s, reference analytic vs its own tight finite diff


@pytest.fixture(scope="module")
def eph_by_prn():
    return ephemeris.parse_rinex(FIX)


@pytest.mark.parametrize("offset_s", [-7200.0, -1800.0, -137.0, 0.0, 512.0, 3600.0, 7200.0])
def test_position_matches_geometry(eph_by_prn, offset_s):
    t = TOE + offset_s
    for prn, eph in eph_by_prn.items():
        p_ref = reference.sat_state(eph, t)["pos"]
        p_prod, _, _ = geometry.sat_state(eph, t)
        err = float(np.linalg.norm(p_ref - np.asarray(p_prod)))
        assert err < POS_TOL_M, f"PRN {prn} @ {offset_s:+.0f}s: {err:.2e} m"


@pytest.mark.parametrize("offset_s", [-3600.0, 0.0, 3600.0])
def test_clock_correction_matches_geometry(eph_by_prn, offset_s):
    t = TOE + offset_s
    for prn, eph in eph_by_prn.items():
        c_ref = reference.sat_state(eph, t)["clock"]
        _, _, c_prod = geometry.sat_state(eph, t)
        assert abs(c_ref - c_prod) < 1e-12, f"PRN {prn}: {abs(c_ref - c_prod):.2e} s"


@pytest.mark.parametrize("offset_s", [-3600.0, 0.0, 1800.0])
def test_reference_velocity_is_analytic_and_self_consistent(eph_by_prn, offset_s):
    """Reference analytic velocity vs a tight central finite difference of
    the reference position -- this is the independent velocity check the
    audit asks for (geometry only ever finite-differences)."""
    t = TOE + offset_s
    h = 0.05
    for prn, eph in eph_by_prn.items():
        v = reference.sat_state(eph, t)["vel"]
        p_plus = reference.sat_state(eph, t + h)["pos"]
        p_minus = reference.sat_state(eph, t - h)["pos"]
        v_fd = (p_plus - p_minus) / (2 * h)
        assert np.linalg.norm(v - v_fd) < VEL_FD_TOL, f"PRN {prn}: analytic vs FD"


@pytest.mark.parametrize("offset_s", [-3600.0, 0.0, 1800.0])
def test_geometry_velocity_agrees_with_reference(eph_by_prn, offset_s):
    t = TOE + offset_s
    for prn, eph in eph_by_prn.items():
        _, v_prod, _ = geometry.sat_state(eph, t)
        v_ref = reference.sat_state(eph, t)["vel"]
        assert np.linalg.norm(np.asarray(v_prod) - v_ref) < VEL_ANALYTIC_TOL, prn


def test_eccentric_anomaly_solvers_agree(eph_by_prn):
    for eph in eph_by_prn.values():
        a = eph["sqrtA"] ** 2
        n = np.sqrt(reference._MU / a ** 3) + eph["delta_n"]
        for tk in (-7200.0, 0.0, 3600.0):
            m = eph["m0"] + n * tk
            e_ref = reference.solve_eccentric_anomaly(m, eph["e"])
            e_prod = geometry._kepler_E(m, eph["e"])
            # both solve E - e sin E = M (mod 2pi); the reference works on
            # the wrapped M, so compare the residual modulo 2pi.
            r_ref = (e_ref - eph["e"] * np.sin(e_ref) - m + np.pi) % (2 * np.pi) - np.pi
            assert abs(r_ref) < 1e-12
            assert abs(np.sin(e_ref) - np.sin(e_prod)) < 1e-9
