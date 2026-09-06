"""SBAS (WAAS/EGNOS/MSAS/...) L1 broadcast-ephemeris propagation.

A RINEX-3 SBAS navigation record carries an ECEF state vector (position,
velocity) plus a constant acceleration, all in the Earth-fixed frame at the
message epoch, and a two-term clock polynomial (aGf0, aGf1). ICAO Annex 10
propagates that state by plain constant-acceleration integration -- there is
NO PZ-90 J2 / centrifugal / Coriolis term (those belong to GLONASS, which is
why ``S`` used to be mis-routed through ``backend.synth.glonass``):

    r(t)   = r0 + v0*dt + 0.5*a*dt**2
    v(t)   = v0 + a*dt
    clk(t) = af0 + af1*dt

``dt == 0`` returns the seed position/velocity/clock exactly so the existing
single-epoch native run stays bit-stable.

The shared broadcast parser (``ephemeris.parse_rinex_multi`` /
``_VARMAP_STATE``) already delivers the state in SI units (m, m/s, m/s**2)
via georinex, so no unit conversion is applied here -- matching
``glonass.glonass_state``'s treatment of the same fields.
"""

from __future__ import annotations

import numpy as np


def sbas_state(record: dict):
    """Return ``f(t_gps) -> (pos_ecef_m_np, vel_ecef_m_s_np, clk_s)`` for an
    SBAS broadcast ``record`` (fields ``x_m,y_m,z_m,vx,vy,vz,ax,ay,az,
    tau,gamma,toe_ref``).

    Closed-form constant-acceleration integration (ICAO Annex 10); RK4 is not
    needed for a constant ``a``.
    """
    r0 = np.array([record["x_m"], record["y_m"], record["z_m"]], dtype=float)
    v0 = np.array([record["vx"], record["vy"], record["vz"]], dtype=float)
    a = np.array([record.get("ax", 0.0), record.get("ay", 0.0),
                  record.get("az", 0.0)], dtype=float)
    t0 = record["toe_ref"]
    af0 = record.get("tau", 0.0)
    af1 = record.get("gamma", 0.0)

    def f(t_gps: float):
        dt = t_gps - t0
        if dt == 0.0:
            return r0.copy(), v0.copy(), float(af0)
        pos = r0 + v0 * dt + 0.5 * a * dt * dt
        vel = v0 + a * dt
        clk = af0 + af1 * dt
        return pos, vel, float(clk)

    return f
