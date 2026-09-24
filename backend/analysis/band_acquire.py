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


def fine_code_phase(iq, fs, code, *, chip_hz, code_len, dopp_hz,
                    center_hz=0.0, nperiods=20) -> float:
    """Sub-sample code phase (chips) for a known Doppler: one non-coherent
    accumulation over ``nperiods`` code periods, then a 3-point parabolic
    interpolation of the correlation peak. band_acquire.acquire only
    resolves the peak to a sample (~0.2 chip at 2.6 MHz / 511.5 kcps)."""
    code = np.asarray(code, dtype=np.float64)
    period_s = code_len / chip_hz
    npp = int(round(fs * period_s))
    nc = min(nperiods, len(iq) // npp)
    if nc < 1:
        return 0.0
    seg = np.asarray(iq[: npp * nc], dtype=np.complex128)
    t = np.arange(npp) / fs
    local = code[np.floor(t * chip_hz).astype(np.int64) % code_len]
    LOC = np.conj(np.fft.fft(local))
    acc = np.zeros(npp)
    for k in range(nc):
        blk = seg[k * npp:(k + 1) * npp]
        ph = np.exp(-1j * 2 * np.pi * (center_hz + dopp_hz) *
                    np.arange(k * npp, (k + 1) * npp) / fs)
        acc += np.abs(np.fft.ifft(np.fft.fft(blk * ph) * LOC))
    p = int(acc.argmax())
    y0, y1, y2 = acc[(p - 1) % npp], acc[p], acc[(p + 1) % npp]
    denom = y0 - 2.0 * y1 + y2
    delta = 0.5 * (y0 - y2) / denom if denom != 0.0 else 0.0
    return float(((p + delta) * chip_hz / fs) % code_len)


# L2C is CM and CL time-multiplexed chip by chip at 1.023 Mcps (IS-GPS-200
# 3.3.2.4): the CM replica holds each CM chip in the even slot and zero in
# the odd (CL) slot. A continuous 511.5 kcps CM replica against that signal
# loses 6 dB and biases the code phase by a quarter CM chip (~150 m).
L2C_TDM_CHIP_HZ = 1.023e6
L2C_TDM_LEN = 20460


def l2c_cm_replica(prn: int) -> np.ndarray:
    """TDM CM replica (float64, 20460 slots at 1.023 Mcps)."""
    cm, _cl = _lib.code_l2c(int(prn))
    rep = np.zeros(L2C_TDM_LEN, dtype=np.float64)
    rep[0::2] = cm
    return rep


def acquire_l2c(iq, fs, prn, *, center_hz=0.0) -> dict:
    """Acquire GPS / QZSS L2C on the CM component (TDM replica, 20 ms
    period). ``code_phase_chips`` is reported in CM chips (511.5 kcps)."""
    r = acquire(iq, fs, l2c_cm_replica(prn), chip_hz=L2C_TDM_CHIP_HZ,
                code_len=L2C_TDM_LEN, center_hz=center_hz, dopp_step=100.0)
    r["code_phase_chips"] = r["code_phase_chips"] / 2.0
    return r
