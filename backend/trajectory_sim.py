"""Analytic reference trajectories for validation.

Each generator returns dense, time-sampled receiver state -- ECEF
position and velocity -- for a named motion profile anchored at a
geodetic origin. These are ground truth for continuity checks and for
driving ``geometry.observables`` to test Doppler/range behaviour without
gps-sdr-sim.

Profiles: static, constant velocity, constant acceleration, circular
(coordinated turn), vertical climb/descent, and a high-dynamics UAV
(figure-of-eight with vertical bob). All are C1-continuous by
construction except where a profile deliberately steps acceleration.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from backend import geometry

_A = 6378137.0
_E2 = 6.69437999014e-3


def _enu_basis(lat_deg, lon_deg):
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    sl, cl, so, co = math.sin(lat), math.cos(lat), math.sin(lon), math.cos(lon)
    e = np.array([-so, co, 0.0])
    n = np.array([-sl * co, -sl * so, cl])
    u = np.array([cl * co, cl * so, sl])
    return e, n, u


@dataclass
class Trajectory:
    name: str
    t_s: np.ndarray
    pos_ecef: np.ndarray            # (N, 3)
    vel_ecef: np.ndarray            # (N, 3)
    pos_enu: np.ndarray             # (N, 3) relative to origin
    meta: dict = field(default_factory=dict)


def _assemble(name, origin_llh, t, enu_pos, enu_vel, meta):
    o = np.array(geometry.llh_to_ecef(*origin_llh))
    e, n, u = _enu_basis(origin_llh[0], origin_llh[1])
    R = np.column_stack([e, n, u])          # ENU -> ECEF
    pos_ecef = o + enu_pos @ R.T
    vel_ecef = enu_vel @ R.T
    return Trajectory(name, t, pos_ecef, vel_ecef, enu_pos, meta)


def generate(profile: str, origin_llh, duration_s: float, rate_hz: float = 10.0,
             **kw) -> Trajectory:
    n = int(round(duration_s * rate_hz)) + 1
    t = np.arange(n) / rate_hz
    z = np.zeros_like(t)

    if profile == "static":
        p = np.column_stack([z, z, z])
        v = np.column_stack([z, z, z])

    elif profile == "constant_velocity":
        vel = np.array(kw.get("velocity_enu", (30.0, 0.0, 0.0)))
        p = np.outer(t, vel)
        v = np.tile(vel, (n, 1))

    elif profile == "constant_acceleration":
        acc = np.array(kw.get("accel_enu", (2.0, 0.0, 0.0)))
        v0 = np.array(kw.get("velocity_enu", (0.0, 0.0, 0.0)))
        p = np.outer(t, v0) + 0.5 * np.outer(t ** 2, acc)
        v = v0 + np.outer(t, acc)

    elif profile == "circular":
        radius = float(kw.get("radius_m", 200.0))
        speed = float(kw.get("speed_mps", 25.0))
        w = speed / radius
        p = np.column_stack([radius * np.cos(w * t) - radius,
                             radius * np.sin(w * t), z])
        v = np.column_stack([-radius * w * np.sin(w * t),
                             radius * w * np.cos(w * t), z])

    elif profile == "vertical":
        rate = float(kw.get("climb_mps", 5.0))
        p = np.column_stack([z, z, rate * t])
        v = np.column_stack([z, z, np.full_like(t, rate)])

    elif profile == "uav_high_dynamics":
        A = float(kw.get("amp_m", 150.0))
        w = float(kw.get("omega", 0.6))
        bob = float(kw.get("bob_m", 20.0))
        p = np.column_stack([A * np.sin(w * t),
                             A * np.sin(w * t) * np.cos(w * t),
                             bob * np.sin(2 * w * t)])
        v = np.column_stack([A * w * np.cos(w * t),
                             A * w * (np.cos(w * t) ** 2 - np.sin(w * t) ** 2),
                             bob * 2 * w * np.cos(2 * w * t)])
    else:
        raise ValueError(f"unknown trajectory profile {profile!r}")

    return _assemble(profile, origin_llh, t, p, v,
                     {"rate_hz": rate_hz, "duration_s": duration_s, **kw})


def continuity_report(traj: Trajectory) -> dict:
    """Numeric continuity of a trajectory: max position jump, agreement
    between the analytic velocity and a finite difference of position,
    and the peak implied acceleration. A discontinuity shows up as a
    position jump many times the median step, or a large FD-vs-analytic
    velocity mismatch."""
    dt = np.diff(traj.t_s)
    dp = np.diff(traj.pos_ecef, axis=0)
    step = np.linalg.norm(dp, axis=1)
    fd_vel = dp / dt[:, None]
    mid_vel = 0.5 * (traj.vel_ecef[:-1] + traj.vel_ecef[1:])
    vel_err = np.linalg.norm(fd_vel - mid_vel, axis=1)
    accel = np.linalg.norm(np.diff(traj.vel_ecef, axis=0) / dt[:, None], axis=1)
    med = float(np.median(step)) or 1e-9
    speed = float(np.max(np.linalg.norm(traj.vel_ecef, axis=1)))
    # relative FD-vs-analytic velocity error: O(dt^2 * jerk / speed), so a
    # genuine step (O(1)) stands out even for high-dynamics profiles.
    rel_vel_err = float(vel_err.max() / (speed + 1e-9))
    return {
        "n": len(traj.t_s),
        "max_step_m": float(step.max()),
        "median_step_m": med,
        "max_step_ratio": float(step.max() / med),
        "max_fd_velocity_error_mps": float(vel_err.max()),
        "rel_fd_velocity_error": rel_vel_err,
        "max_acceleration_mps2": float(accel.max()),
        "continuous": bool(step.max() < 5.0 * med and rel_vel_err < 5e-3),
    }


def doppler_series(traj: Trajectory, eph, t_gps0: float) -> np.ndarray:
    """L1 carrier Doppler (Hz) for one satellite along the trajectory,
    including the receiver-velocity term."""
    out = np.empty(len(traj.t_s))
    for k, (p, v) in enumerate(zip(traj.pos_ecef, traj.vel_ecef)):
        o = geometry.observables(eph, p, t_gps0 + traj.t_s[k], rx_vel=v)
        out[k] = o["carrier_doppler_hz"]
    return out


def doppler_continuity(series: np.ndarray, dt_s: float,
                       outlier_ratio: float = 8.0) -> dict:
    """Check a Doppler series has no steps. Scale-free: the largest
    sample-to-sample change must not be a wild outlier against the
    median change (a real discontinuity spikes one difference; smooth
    high-dynamics motion keeps them comparable). The second difference
    (Doppler acceleration) gets the same outlier test."""
    d1 = np.abs(np.diff(series) / dt_s)
    d2 = np.abs(np.diff(series, 2) / dt_s ** 2)
    med1 = float(np.median(d1)) or 1e-9
    med2 = float(np.median(d2)) or 1e-9
    return {
        "max_abs_doppler_hz": float(np.max(np.abs(series))),
        "max_doppler_rate_hz_per_s": float(d1.max()),
        "rate_outlier_ratio": float(d1.max() / med1),
        "accel_outlier_ratio": float(d2.max() / med2),
        "continuous": bool(d1.max() / med1 < outlier_ratio
                           and d2.max() / med2 < outlier_ratio),
    }
