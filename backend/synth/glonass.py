"""GLONASS G1 broadcast-ephemeris propagation.

A GLONASS navigation record carries an ECEF state vector (position, velocity)
plus a constant luni-solar acceleration, all in the rotating PZ-90.11 frame at
the broadcast epoch. This module propagates that state with the RK4 integrator
of the GLONASS ICD "simplified equations of motion" (Appendix): a point-mass
term, the J2 zonal term, and -- because the integration frame co-rotates with
the Earth -- centrifugal (OMEGA**2 * x,y) and Coriolis (+/- 2 OMEGA v) terms,
with the broadcast ax/ay/az added as a frame-constant luni-solar acceleration.

Time scale note (unresolved here -- TODO Task 16, correlated-epoch wiring):
    georinex returns GLONASS broadcast epochs on the UTC(SU) scale, so a raw
    parsed record's ``toe_ref`` (from ``ephemeris.parse_rinex_multi``) is UTC
    seconds-of-week, whereas ``t_gps`` passed to ``f()`` is GPS seconds-of-week
    (GPS = UTC + config.GPS_UTC_LEAP_S, currently 18 s).

    This integrator only ever uses ``dt_total = t_gps - toe_ref`` -- a true
    elapsed interval whenever both endpoints share a scale. In the production
    path that holds: ``ephemeris.align_epochs`` rewrites an R/S record's
    ``toe_ref`` to the requested GPS start SoW before propagation, so the
    subtraction is GPS-minus-GPS. In the raw-parse path (used by these unit
    tests) both endpoints are UTC-scale because callers evaluate relative to
    ``toe_ref`` itself.

    The unhandled case is a *raw, unaligned* GLONASS record evaluated at a
    genuine GPS SoW alongside GPS satellites -- there ``dt_total`` would carry
    an ~18 s error (~60 km). Task 16 must guarantee alignment (or subtract
    ``config.GPS_UTC_LEAP_S`` here) before mixing GLONASS into a correlated
    multi-GNSS epoch. No leap term is applied unconditionally because
    ``f(toe_ref)`` must return the broadcast state exactly (dt_total == 0).
"""

from __future__ import annotations

import numpy as np

MU_PZ90 = 3.9860044e14
A_E = 6378136.0
J2 = 1.0826257e-3
OMEGA_PZ90 = 7.292115e-5
OMEGA = OMEGA_PZ90

_STEP_S = 60.0


def _accel(y, acc_luni):
    x, yy, z = y[0], y[1], y[2]
    r = np.sqrt(x * x + yy * yy + z * z)
    mu_r3 = MU_PZ90 / r ** 3
    zr2 = (z / r) ** 2
    j2f = 1.5 * J2 * mu_r3 * (A_E / r) ** 2
    ax = -mu_r3 * x + j2f * x * (5.0 * zr2 - 1.0) + OMEGA ** 2 * x + 2.0 * OMEGA * y[4] + acc_luni[0]
    ay = -mu_r3 * yy + j2f * yy * (5.0 * zr2 - 1.0) + OMEGA ** 2 * yy - 2.0 * OMEGA * y[3] + acc_luni[1]
    az = -mu_r3 * z + j2f * z * (5.0 * zr2 - 3.0) + acc_luni[2]
    return np.array([y[3], y[4], y[5], ax, ay, az])


def _rk4_step(y, dt, acc_luni):
    k1 = _accel(y, acc_luni)
    k2 = _accel(y + 0.5 * dt * k1, acc_luni)
    k3 = _accel(y + 0.5 * dt * k2, acc_luni)
    k4 = _accel(y + dt * k3, acc_luni)
    return y + (dt / 6.0) * (k1 + 2.0 * k2 + 2.0 * k3 + k4)


def glonass_state(record: dict):
    """Return ``f(t_gps) -> (pos_ecef_m_np, vel_ecef_m_s_np, clk_s)`` for a
    GLONASS/SBAS broadcast ``record`` (fields ``x_m,y_m,z_m,vx,vy,vz,
    ax,ay,az,tau,gamma,toe_ref``).

    georinex already delivers the state in SI units (m, m/s, m/s**2).
    """
    y0 = np.array([record["x_m"], record["y_m"], record["z_m"],
                   record["vx"], record["vy"], record["vz"]], dtype=float)
    acc = np.array([record.get("ax", 0.0), record.get("ay", 0.0),
                    record.get("az", 0.0)], dtype=float)
    t0 = record["toe_ref"]
    tau = record.get("tau", 0.0)
    gamma = record.get("gamma", 0.0)

    def f(t_gps: float):
        # dt_total is a true elapsed interval only when t_gps and t0 share a
        # time scale; see the module docstring / TODO Task 16.
        dt_total = t_gps - t0
        y = y0.copy()
        step = _STEP_S if dt_total >= 0.0 else -_STEP_S
        remaining = dt_total
        while abs(remaining) > 1e-9:
            h = step if abs(remaining) > abs(step) else remaining
            y = _rk4_step(y, h, acc)
            remaining -= h
        clk = -tau + gamma * dt_total
        return y[:3].copy(), y[3:].copy(), float(clk)

    return f
