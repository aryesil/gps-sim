# SPDX-License-Identifier: MIT
"""Run the gnss_rx receiver on an int16 IQ file and estimate the oscillator error y per second.

    python scripts/gnss_rx_validate.py out/<run>/gpssim.bin 2026-10-20T06:30:00 39.9179 32.8627 850 \
        --fs 4e6 --seconds 15

Truth Doppler is predicted from the day's broadcast ephemeris (backend.ephem). Used to check the
receiver chain against simulator IQ with an injected ``receiver_clock`` drift.
"""
from __future__ import annotations

import argparse
import datetime as dt
import warnings

import numpy as np

from backend.rf.doppler import ecef as _ecef, make_predictor
from gnss_rx.acquire import acquire
from gnss_rx.clock import estimate_clock
from gnss_rx.track import Tracker


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bin")
    ap.add_argument("start_utc")
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("alt", type=float)
    ap.add_argument("--fs", type=float, default=4e6)
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--prns", type=int, nargs="*", default=list(range(1, 33)))
    a = ap.parse_args()
    fs = a.fs
    raw = np.memmap(a.bin, dtype=np.int16, mode="r")
    nsamp = min(len(raw) // 2, int(fs * a.seconds))

    def block(i0, n):
        v = np.asarray(raw[2 * i0:2 * (i0 + n)], np.float32)
        return v[0::2] + 1j * v[1::2]

    first = block(0, int(fs * 12e-3))
    trs = {}
    for prn in a.prns:
        r = acquire(first, fs, prn)
        if r.metric > 3.0:
            print(f"acq PRN {prn:2d} doppler {r.doppler_hz:+8.0f} metric {r.metric:5.1f}")
            t = Tracker(fs, prn, r.doppler_hz, r.code_phase_samples)
            t.push(first[r.code_phase_samples:])
            trs[prn] = t
    pos = int(fs * 12e-3)
    step = int(fs * 0.1)
    while pos < nsamp:
        n = min(step, nsamp - pos)
        blk = block(pos, n)
        for t in trs.values():
            t.push(blk)
            t.step()
        pos += n

    start = dt.datetime.fromisoformat(a.start_utc)
    from backend.ephem.ephemeris import get_ephemeris
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        eph = get_ephemeris(start.date(), download=False)
    predict = make_predictor(start, _ecef(a.lat, a.lon, a.alt), eph)
    ys = []
    y_prior = 0.0
    for w0 in np.arange(4.0, a.seconds - 1.0, 1.0):
        fix = None
        for _ in range(3):
            fix = estimate_clock(trs, predict, fs, w0, w0 + 1.0, y_prior=y_prior)
            if fix is None:
                break
            y_prior = fix.y
        if fix:
            ys.append(fix.y)
            print(f"t={w0:5.1f}s y={fix.y * 1e6:+.6f} ppm sigma={fix.sigma * 1e9:.3f} ppb nsv={len(fix.used)} rej={fix.rejected}")
    if ys:
        ys = np.array(ys)
        print(f"mean y = {ys.mean() * 1e6:+.6f} ppm, std over 1 s windows = {ys.std() * 1e9:.3f} ppb")


if __name__ == "__main__":
    main()
