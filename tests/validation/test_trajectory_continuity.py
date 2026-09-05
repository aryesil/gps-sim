"""Analytic trajectories are C1-continuous and produce continuous
Doppler when propagated through geometry."""
import math
import pathlib

import numpy as np
import pytest

from backend import ephemeris, trajectory_sim as T

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "brdc_sample.rnx"
ORIGIN = (41.0082, 28.9784, 120.0)
TOE = 475200.0

PROFILES = [
    ("static", {}),
    ("constant_velocity", {"velocity_enu": (45.0, 10.0, 0.0)}),
    ("constant_acceleration", {"accel_enu": (3.0, 0.0, -1.0)}),
    ("circular", {"radius_m": 150.0, "speed_mps": 30.0}),
    ("vertical", {"climb_mps": 8.0}),
    ("uav_high_dynamics", {"amp_m": 120.0, "omega": 0.8}),
]


@pytest.fixture(scope="module")
def eph_by_prn():
    return ephemeris.parse_rinex(FIX)


@pytest.mark.parametrize("profile,kw", PROFILES)
def test_trajectory_is_continuous(profile, kw):
    traj = T.generate(profile, ORIGIN, duration_s=30.0, rate_hz=20.0, **kw)
    rep = T.continuity_report(traj)
    assert rep["continuous"], rep
    assert rep["rel_fd_velocity_error"] < 5e-3
    assert rep["max_step_ratio"] < 5.0


@pytest.mark.parametrize("profile,kw", PROFILES)
def test_velocity_matches_position_derivative(profile, kw):
    traj = T.generate(profile, ORIGIN, duration_s=20.0, rate_hz=50.0, **kw)
    dt = np.diff(traj.t_s)[:, None]
    fd = np.diff(traj.pos_ecef, axis=0) / dt
    mid = 0.5 * (traj.vel_ecef[:-1] + traj.vel_ecef[1:])
    assert np.max(np.linalg.norm(fd - mid, axis=1)) < 0.02


def test_static_trajectory_does_not_move():
    traj = T.generate("static", ORIGIN, duration_s=10.0)
    assert np.allclose(traj.pos_ecef, traj.pos_ecef[0])
    assert np.allclose(traj.vel_ecef, 0.0)


def test_constant_velocity_speed_is_exact():
    traj = T.generate("constant_velocity", ORIGIN, duration_s=10.0,
                      velocity_enu=(30.0, 40.0, 0.0))
    speed = np.linalg.norm(traj.vel_ecef, axis=1)
    assert np.allclose(speed, 50.0, atol=1e-6)


def test_circular_stays_on_its_radius():
    traj = T.generate("circular", ORIGIN, duration_s=40.0, rate_hz=20.0,
                      radius_m=200.0, speed_mps=25.0)
    # ENU position is a circle of radius R centred at (-R, 0)
    d = np.linalg.norm(traj.pos_enu[:, :2] - np.array([-200.0, 0.0]), axis=1)
    assert np.allclose(d, 200.0, atol=1e-6)


@pytest.mark.parametrize("profile,kw", PROFILES)
def test_doppler_series_is_continuous(profile, kw, eph_by_prn):
    traj = T.generate(profile, ORIGIN, duration_s=20.0, rate_hz=10.0, **kw)
    eph = next(iter(eph_by_prn.values()))
    series = T.doppler_series(traj, eph, TOE)
    rep = T.doppler_continuity(series, dt_s=0.1)
    assert rep["continuous"], rep
    assert rep["max_abs_doppler_hz"] < 10_000.0


def test_high_dynamics_has_larger_doppler_swing_than_static(eph_by_prn):
    eph = next(iter(eph_by_prn.values()))
    stat = T.doppler_series(T.generate("static", ORIGIN, 20.0, 10.0), eph, TOE)
    uav = T.doppler_series(
        T.generate("uav_high_dynamics", ORIGIN, 20.0, 10.0, amp_m=150.0, omega=1.0),
        eph, TOE)
    assert np.ptp(uav) > np.ptp(stat) + 50.0
