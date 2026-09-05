"""Independent reference implementation of GPS broadcast-ephemeris propagation.

This module deliberately does **not** import :mod:`backend.geometry`. It is a
second, independently written implementation of the IS-GPS-200 Table 20-IV
satellite position/velocity algorithm, used by the validation test-suite to
check ``backend.geometry`` (the production path that actually feeds
``gps-sdr-sim`` and the receiver) against code that shares none of its
helpers and therefore cannot share its bugs (see the project audit,
"NO SELF-VALIDATING ARCHITECTURE").

Deliberate implementation differences that make the cross-check meaningful:

* eccentric anomaly by Halley iteration from a conditional seed, not the
  Newton iteration in :func:`geometry._kepler_E`;
* satellite velocity from the closed-form analytic derivative of the
  Kepler + harmonic model (Remondi 2004), not the 0.5 s central finite
  difference in :func:`geometry.sat_state`;
* ECEF assembled with explicit 3x3 rotation matrices;
* the transmit-time / Sagnac loop is written from scratch.

Physical constants are intentionally the same as :mod:`backend.config` -- a
reference model must use the same physics, only different code.
"""
from __future__ import annotations

import numpy as np

from backend import config

_MU = config.MU
_OMEGA_E = config.OMEGA_E_DOT
_C = config.C
_F_REL = config.F_REL
_L1 = config.L1_HZ
_HALF_WEEK = 302400.0
_WEEK = 604800.0


def _wrap_dt(seconds: float) -> float:
    """Fold a GPS time difference into (-half week, +half week]."""
    if seconds > _HALF_WEEK:
        seconds -= _WEEK
    elif seconds < -_HALF_WEEK:
        seconds += _WEEK
    return seconds


def solve_eccentric_anomaly(mean_anomaly: float, e: float,
                            tol: float = 1e-14, max_iter: int = 60) -> float:
    """Solve Kepler's equation ``E - e*sin E = M`` by Halley's method.

    Cubic convergence from a seed chosen by eccentricity; independent of the
    Newton loop in :func:`backend.geometry._kepler_E`.
    """
    m = (mean_anomaly + np.pi) % (2 * np.pi) - np.pi
    e_anom = m if e < 0.8 else np.pi * (1.0 if m >= 0.0 else -1.0)
    for _ in range(max_iter):
        f = e_anom - e * np.sin(e_anom) - m
        fp = 1.0 - e * np.cos(e_anom)
        fpp = e * np.sin(e_anom)
        step = -f / (fp - 0.5 * f * fpp / fp)
        e_anom += step
        if abs(step) < tol:
            break
    return e_anom


def _rot1(theta: float) -> np.ndarray:
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, s], [0.0, -s, c]])


def _rot3(theta: float) -> np.ndarray:
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, s, 0.0], [-s, c, 0.0], [0.0, 0.0, 1.0]])


def sat_state(eph: dict, t_gps: float) -> dict:
    """IS-GPS-200 broadcast propagation at GPS time ``t_gps`` (seconds of
    week, same convention as :func:`geometry.sat_state`).

    Returns a dict with ECEF ``pos`` (m) and ``vel`` (m/s), the satellite
    ``clock`` correction (s, relativistic term included, group delay not),
    and the intermediate ``E`` / ``nu`` / ``tk`` for white-box assertions.
    """
    a = eph["sqrtA"] ** 2
    e = eph["e"]
    n0 = np.sqrt(_MU / a ** 3)
    tk = _wrap_dt(t_gps - eph["toe"])
    n = n0 + eph["delta_n"]
    mk = eph["m0"] + n * tk
    ek = solve_eccentric_anomaly(mk, e)
    sin_e, cos_e = np.sin(ek), np.cos(ek)

    nu = np.arctan2(np.sqrt(1.0 - e * e) * sin_e, cos_e - e)
    phi = nu + eph["omega"]
    s2, c2 = np.sin(2.0 * phi), np.cos(2.0 * phi)

    du = eph["cus"] * s2 + eph["cuc"] * c2
    dr = eph["crs"] * s2 + eph["crc"] * c2
    di = eph["cis"] * s2 + eph["cic"] * c2

    u = phi + du
    r = a * (1.0 - e * cos_e) + dr
    inc = eph["i0"] + eph["idot"] * tk + di
    omega = eph["omega0"] + (eph["omega_dot"] - _OMEGA_E) * tk - _OMEGA_E * eph["toe"]

    xk, yk = r * np.cos(u), r * np.sin(u)               # in-plane
    pos = _rot3(-omega) @ _rot1(-inc) @ np.array([xk, yk, 0.0])

    # --- analytic velocity (Remondi 2004), fully independent of geometry ---
    ek_dot = n / (1.0 - e * cos_e)
    nu_dot = ek_dot * np.sqrt(1.0 - e * e) / (1.0 - e * cos_e)
    u_dot = nu_dot + 2.0 * nu_dot * (eph["cus"] * c2 - eph["cuc"] * s2)
    r_dot = a * e * sin_e * ek_dot + 2.0 * nu_dot * (eph["crs"] * c2 - eph["crc"] * s2)
    i_dot = eph["idot"] + 2.0 * nu_dot * (eph["cis"] * c2 - eph["cic"] * s2)
    omega_dot_k = eph["omega_dot"] - _OMEGA_E

    xk_dot = r_dot * np.cos(u) - r * u_dot * np.sin(u)
    yk_dot = r_dot * np.sin(u) + r * u_dot * np.cos(u)
    cO, sO = np.cos(omega), np.sin(omega)
    ci, si = np.cos(inc), np.sin(inc)

    vx = (xk_dot * cO - yk_dot * ci * sO + yk * si * sO * i_dot
          - (xk * sO + yk * ci * cO) * omega_dot_k)
    vy = (xk_dot * sO + yk_dot * ci * cO - yk * si * cO * i_dot
          + (xk * cO - yk * ci * sO) * omega_dot_k)
    vz = yk_dot * si + yk * ci * i_dot
    vel = np.array([vx, vy, vz])

    tsv = _wrap_dt(t_gps - eph["toc"])
    clock = (eph["af0"] + eph["af1"] * tsv + eph["af2"] * tsv ** 2
             + _F_REL * e * eph["sqrtA"] * sin_e)

    return {"pos": pos, "vel": vel, "clock": float(clock),
            "E": float(ek), "nu": float(nu), "tk": float(tk)}


def _sagnac(pos: np.ndarray, theta: float) -> np.ndarray:
    """Rotate an ECEF vector by Earth rotation ``theta`` (rad) accumulated
    during signal flight, i.e. express the transmit-epoch ECEF position in
    the receive-epoch ECEF frame."""
    c, s = np.cos(theta), np.sin(theta)
    return np.array([c * pos[0] + s * pos[1], -s * pos[0] + c * pos[1], pos[2]])


def solve_transmit_time(eph: dict, rx_ecef, t_rx: float, iters: int = 15) -> dict:
    """Light-time + Sagnac solution, written independently of
    :func:`geometry.solve_transmit_time`.

    Returns ``sat_ecef`` (Sagnac-corrected, receive-epoch frame),
    ``geo_range_m``, ``tof_s``, satellite ``clock_s``, ECEF ``sat_vel``, and
    ``range_rate_mps`` (range rate along the line of sight, receiver assumed
    stationary).
    """
    rx = np.asarray(rx_ecef, float)
    tof = np.linalg.norm(sat_state(eph, t_rx)["pos"] - rx) / _C
    st = None
    pos_rot = None
    for _ in range(iters):
        st = sat_state(eph, t_rx - tof)
        pos_rot = _sagnac(st["pos"], _OMEGA_E * tof)
        tof = np.linalg.norm(pos_rot - rx) / _C
    los_vec = pos_rot - rx
    geo = float(np.linalg.norm(los_vec))
    los = los_vec / geo
    vel_rot = _sagnac(st["vel"], _OMEGA_E * tof)
    range_rate = float(vel_rot @ los)
    return {
        "sat_ecef": pos_rot, "sat_vel": vel_rot,
        "geo_range_m": geo, "tof_s": float(tof),
        "clock_s": st["clock"], "range_rate_mps": range_rate,
        "carrier_doppler_hz": float(-_L1 * range_rate / _C),
    }
