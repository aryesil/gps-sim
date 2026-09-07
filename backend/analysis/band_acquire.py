"""Non-frozen FFT acquisition correlator for the L2 / L5 bands.

Deliberately NOT in ``backend/inspector.py`` (frozen, GPS L1 C/A only).
Per signal: an explicit ``{-1,+1}`` primary-code array, its chip rate and
length, and a band-centre offset; a code-phase x Doppler search returning
a peak-to-noise metric in dB. Shares no DSP with ``inspector`` -- only the
compiled-in ranging-code tables are common, preserving the self-validation
property (the generator and the correlator agree only through published
constants).
"""
from __future__ import annotations

import numpy as np

from backend.synth import _lib


def acquire(iq, fs, code, *, chip_hz, code_len, center_hz=0.0,
            dopp_hz=6000.0, dopp_step=200.0, nperiods=4) -> dict:
    """{'metric_db', 'doppler_hz', 'code_phase_chips'} for one SV.

    ``code`` is a length-``code_len`` array in ``{-1,+1}``. Non-coherent
    accumulation over up to ``nperiods`` code periods; the noise floor is
    the median of the accumulator.
    """
    code = np.asarray(code, dtype=np.float64)
    period_s = code_len / chip_hz
    npp = int(round(fs * period_s))
    avail = len(iq) // npp if npp else 0
    if avail < 1:
        return {"metric_db": -99.0, "doppler_hz": 0.0, "code_phase_chips": 0.0}
    nc = min(nperiods, avail)
    seg = np.asarray(iq[: npp * nc], dtype=np.complex128)

    t = np.arange(npp) / fs
    idx = np.floor(t * chip_hz).astype(np.int64) % code_len
    local = code[idx]
    LOC = np.conj(np.fft.fft(local))

    best_pk, best, best_floor = -1.0, (0.0, 0), 1.0
    for fd in np.arange(-dopp_hz, dopp_hz + 1, dopp_step):
        acc = np.zeros(npp)
        for k in range(nc):
            blk = seg[k * npp:(k + 1) * npp]
            ph = np.exp(-1j * 2 * np.pi * (center_hz + fd) *
                        np.arange(k * npp, (k + 1) * npp) / fs)
            acc += np.abs(np.fft.ifft(np.fft.fft(blk * ph) * LOC)) ** 2
        pk = acc.max()
        if pk > best_pk:
            best_pk = pk
            best = (float(fd), int(acc.argmax()))
            best_floor = float(np.median(acc))
    return {
        "metric_db": float(10 * np.log10(best_pk / max(best_floor, 1e-9))),
        "doppler_hz": best[0],
        "code_phase_chips": (best[1] * chip_hz / fs) % code_len,
    }


def acquire_l2c(iq, fs, prn, *, center_hz=0.0) -> dict:
    """Acquire GPS / QZSS L2C on the CM component (10230 chips, 511.5 kcps,
    20 ms period)."""
    cm, _cl = _lib.code_l2c(int(prn))
    return acquire(iq, fs, cm.astype(np.float64), chip_hz=0.5115e6,
                   code_len=10230, center_hz=center_hz, dopp_step=100.0)
