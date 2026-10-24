"""Predicted satellite Doppler at a fixed receiver, from the day's broadcast ephemeris.

Used by the board's own oscillator measurement: the receiver position and an approximate
UTC are known to a few hundred metres and well under a second, which is enough for a
Doppler prediction good to a small fraction of a hertz.
"""
from __future__ import annotations

import datetime as dt

import numpy as np

from gnss_rx import C, L1_HZ

GPS_MINUS_UTC = 18.0


def ecef(lat_deg: float, lon_deg: float, h_m: float) -> np.ndarray:
    a, f = 6378137.0, 1 / 298.257223563
    e2 = f * (2 - f)
    lat, lon = np.radians(lat_deg), np.radians(lon_deg)
    n = a / np.sqrt(1 - e2 * np.sin(lat) ** 2)
    return np.array([(n + h_m) * np.cos(lat) * np.cos(lon), (n + h_m) * np.cos(lat) * np.sin(lon),
                     (n * (1 - e2) + h_m) * np.sin(lat)])


def make_predictor(start_utc: dt.datetime, rx: np.ndarray, eph: dict):
    """``predict(prn, t_rx)``: Doppler in Hz at L1, ``t_rx`` seconds after ``start_utc``."""
    from backend import config, geometry
    w = config.OMEGA_E_DOT
    gps0 = start_utc + dt.timedelta(seconds=GPS_MINUS_UTC)
    sow0 = (gps0.weekday() + 1) % 7 * 86400 + gps0.hour * 3600 + gps0.minute * 60 + gps0.second \
        + gps0.microsecond * 1e-6

    def rng(e, t):
        tof = 0.075
        for _ in range(6):
            pos, _v, _c = geometry.sat_state(e, t - tof)
            th = w * tof
            p = np.array([np.cos(th) * pos[0] + np.sin(th) * pos[1],
                          -np.sin(th) * pos[0] + np.cos(th) * pos[1], pos[2]])
            tof = np.linalg.norm(p - rx) / C
        return tof * C

    lam = C / L1_HZ

    def predict(prn, t_rx):
        t = sow0 + t_rx
        e = eph[prn]
        return -(rng(e, t + 0.5) - rng(e, t - 0.5)) / lam
    return predict
