# SPDX-License-Identifier: MIT
"""Receiver oscillator error from tracked carrier phase and predicted satellite Doppler.

The receiver LO and sample clock both derive from one oscillator with fractional error y
(positive = fast). A fast LO sits above the nominal L1 frequency, so every signal appears
shifted by -y * L1 in baseband. With predicted Doppler d_pred(prn, t) (position, ephemeris,
satellite clock rate known) and measured baseband frequency f_meas:

    y = (d_pred - f_meas_true_time) / L1,   f_meas_true_time = f_meas * (1 + y)

because a fast clock makes the receiver's second shorter than a true second.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import L1_HZ


@dataclass
class ClockFix:
    y: float                    # fractional frequency error, dimensionless
    sigma: float                # 1-sigma of the weighted mean (dimensionless)
    used: list                  # PRNs used
    rejected: list              # PRNs rejected as outliers
    per_sv: dict                # prn -> y_sv


def carrier_doppler(epochs, t0, t1, fs):
    """Least-squares slope of carrier phase (cycles) against receiver time in [t0, t1) seconds.
    Returns (freq_hz, n_epochs, mean_cn0, locked_fraction)."""
    ep = [e for e in epochs if t0 <= e.sample_index / fs < t1]
    if len(ep) < 50:
        return None
    t = np.array([e.sample_index / fs for e in ep])
    ph = np.array([e.carrier_cycles for e in ep])
    tm = t.mean()
    slope = np.polyfit(t - tm, ph, 1)[0]
    return float(slope), len(ep), float(np.mean([e.cn0_dbhz for e in ep])), float(np.mean([e.locked for e in ep]))


def estimate_clock(trackers: dict, predict, fs: float, t0: float, t1: float, y_prior: float = 0.0,
                   min_cn0: float = 30.0, sigma_f_floor_hz: float = 0.01) -> ClockFix | None:
    """``trackers``: prn -> Tracker. ``predict(prn, t_mid)``: predicted Doppler at L1 in Hz at the
    receiver time ``t_mid`` (seconds, receiver sample clock)."""
    per, sig = {}, {}
    tm = 0.5 * (t0 + t1)
    for prn, tr in trackers.items():
        r = carrier_doppler(tr.epochs, t0, t1, fs)
        if r is None:
            continue
        f, n, cn0, lockf = r
        if cn0 < min_cn0 or lockf < 0.9:
            continue
        pred = predict(prn, tm)
        y_sv = (pred - f * (1.0 + y_prior)) / L1_HZ
        per[prn] = y_sv
        # carrier-phase slope noise over the window falls with cn0 and window length
        cn0_lin = 10.0 ** (cn0 / 10.0)
        sig[prn] = max(sigma_f_floor_hz, 1.0 / np.sqrt(cn0_lin) / (t1 - t0) ** 1.5) / L1_HZ
    if not per:
        return None
    prns = list(per)
    vals = np.array([per[p] for p in prns])
    sg = np.array([sig[p] for p in prns])
    rejected = []
    if len(prns) >= 4:
        med = np.median(vals)
        mad = 1.4826 * np.median(np.abs(vals - med))
        lim = max(5.0 * mad, 0.5 / L1_HZ)
        keep = np.abs(vals - med) <= lim
        rejected = [p for p, k in zip(prns, keep) if not k]
        prns = [p for p, k in zip(prns, keep) if k]
        vals, sg = vals[keep], sg[keep]
    w = 1.0 / sg ** 2
    y = float(np.sum(w * vals) / np.sum(w))
    sigma = float(1.0 / np.sqrt(np.sum(w)))
    return ClockFix(y, sigma, prns, rejected, {p: per[p] for p in per})
