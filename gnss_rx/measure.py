# SPDX-License-Identifier: MIT
"""End to end oscillator error measurement from a stream of baseband IQ.

Acquire every candidate PRN on the first samples, track the ones found, then estimate the
receiver clock error ``y`` once per window with ``estimate_clock``. The caller supplies the
IQ source (any callable returning the next ``n`` complex samples) and the predicted Doppler
``predict(prn, t_rx)`` for the receiver position and the broadcast ephemeris.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .acquire import acquire, acquire_weak_many
from .clock import ClockFix, estimate_clock
from .track import Tracker


@dataclass
class Measurement:
    y: float                                   # mean over the windows, dimensionless (+ = fast)
    sigma: float                               # standard error of the mean over windows
    spread: float                              # std over the windows
    acquired: list                             # PRNs found
    windows: list = field(default_factory=list)   # (t_mid, ClockFix)


def _fix(trackers, predict, fs, t0, t1, y0):
    """Iterate because the time-dilation term depends on y itself."""
    fix, prior = None, y0
    for _ in range(3):
        fix = estimate_clock(trackers, predict, fs, t0, t1, y_prior=prior)
        if fix is None:
            return None
        prior = fix.y
    return fix


def measure_oscillator(read, fs: float, predict, prns, seconds: float, window_s: float = 1.0,
                       settle_s: float = 4.0, block_s: float = 0.1, acq_ms: float = 12.0,
                       min_metric: float = 3.0, y0: float = 0.0, weak: bool = False,
                       weak_noncoh: int = 30, progress=None) -> Measurement | None:
    """``read(n)``: next ``n`` IQ samples. ``settle_s``: tracking time dropped before the first
    window (FLL pull-in). ``weak``: long-integration acquisition (``weak_noncoh`` x 10 ms,
    +-12 kHz) for weak signals instead of 12 ms / +-10 kHz. ``progress(event, **info)`` is called
    as the measurement runs: ``"acquired"`` (``found``: prn -> (metric, doppler_hz, tracked)),
    ``"window"`` (``t``, ``fix``, ``trackers``) and ``"tracking"`` (``t``, ``trackers``) about once
    per second. Returns None when nothing is acquired or no window produced a fix."""
    if weak:
        acq_ms = 10.0 * weak_noncoh
    first = np.asarray(read(int(fs * acq_ms * 1e-3)))
    trackers = {}
    prns = list(prns)
    found = acquire_weak_many(first, fs, prns, n_noncoh=weak_noncoh) if weak else [acquire(first, fs, p) for p in prns]
    if progress is not None:
        progress("acquired", found={r.prn: (r.metric, r.doppler_hz, r.metric > min_metric) for r in found})
    for r in found:
        prn = r.prn
        if r.metric > min_metric:
            t = Tracker(fs, prn, r.doppler_hz, r.code_phase_samples)
            t.push(first[r.code_phase_samples:])
            trackers[prn] = t
    if not trackers:
        return None
    n_total = int(fs * seconds)
    n = len(first)
    step = int(fs * block_s)
    windows, ys = [], []
    state = {"t0": settle_s, "prior": y0, "tick": 0.0}

    def drain(limit):
        """Compute every window that ends before ``limit`` seconds of receiver time."""
        while state["t0"] + window_s <= limit and state["t0"] + window_s <= seconds - 0.5:
            t0 = state["t0"]
            fix = _fix(trackers, predict, fs, t0, t0 + window_s, state["prior"])
            if fix is not None:
                state["prior"] = fix.y
                ys.append(fix.y)
                windows.append((t0 + 0.5 * window_s, fix))
                if progress is not None:
                    progress("window", t=t0 + 0.5 * window_s, fix=fix, trackers=trackers)
            state["t0"] = t0 + window_s

    while n < n_total:
        m = min(step, n_total - n)
        blk = np.asarray(read(m))
        for t in trackers.values():
            t.push(blk)
            t.step()
        n += m
        if progress is not None:
            drain(n / fs - 0.5)                    # the last half second of epochs may still be pending
            if n / fs - state["tick"] >= 1.0:
                state["tick"] = n / fs
                progress("tracking", t=n / fs, trackers=trackers)
    drain(seconds)
    if not ys:
        return None
    ys = np.asarray(ys)
    spread = float(ys.std(ddof=1)) if len(ys) > 1 else float("nan")
    sem = spread / np.sqrt(len(ys)) if len(ys) > 1 else float("nan")
    return Measurement(float(ys.mean()), float(sem), spread, sorted(trackers), windows)
