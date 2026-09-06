"""Tiny FFT acquisition correlator for the multi-GNSS band-IQ tests.

Deliberately NOT in backend/ (RULING R4: inspector.py is frozen). Per-PRN,
takes an explicit primary-code length / chip rate, does a code-phase x Doppler
search and returns a peak-to-noise metric in dB.

Galileo E1 is BOC(1,1): we correlate against the plain E1C pilot primary code
(``_lib.code(6, prn, 4092, 25)`` primary, secondary dropped) with NO BOC
replica. A plain-primary correlation of a BOC(1,1) signal still produces clear
peaks (split ~+/-0.5 chip), which is enough for a presence/acquisition check in
a noiseless sim; it is not a code-phase-accurate tracking correlator.
"""
from __future__ import annotations

import numpy as np

from backend.synth import _lib

# sys letter -> (code-gen enum for _lib.code, primary length, chip rate Hz)
_CODEGEN = {
    "G": (0, 1023, 1_023_000.0),
    "J": (1, 1023, 1_023_000.0),
    "S": (2, 1023, 1_023_000.0),
    "C": (3, 2046, 2_046_000.0),
    "E": (6, 4092, 1_023_000.0),   # E1C pilot primary, no BOC
    "R": (4, 511, 511_000.0),
}


def primary_code(sysc: str, prn: int) -> np.ndarray:
    if sysc == "R":
        from backend.synth.engine import _glo_g1_code
        return _glo_g1_code().astype(np.float64)
    csys, clen, _ = _CODEGEN[sysc]
    sec_len = 25 if sysc == "E" else 0
    prim, _sec = _lib.code(csys, prn, clen, sec_len)
    return prim.astype(np.float64)


def acquire(iq, fs, sysc, prn, *, code_len=None, chip_hz=None,
            center_hz=0.0, dopp_hz=5000.0, dopp_step=200.0, nperiods=4):
    """Return {'metric_db', 'doppler_hz', 'code_phase_chips'} for one SV."""
    if code_len is None or chip_hz is None:
        _c, code_len, chip_hz = _CODEGEN[sysc]
    code = primary_code(sysc, prn)

    period_s = code_len / chip_hz
    npp = int(round(fs * period_s))              # samples per code period
    avail = len(iq) // npp
    if avail < 1:
        return {"metric_db": -99.0, "doppler_hz": 0.0, "code_phase_chips": 0.0}
    nc = min(nperiods, avail)
    seg = np.asarray(iq[: npp * nc], dtype=np.complex128)

    # local replica: one code period resampled to npp samples
    t = np.arange(npp) / fs
    idx = np.floor(t * chip_hz).astype(int) % code_len
    local = code[idx]
    LOC = np.conj(np.fft.fft(local))

    n = np.arange(npp)
    dopps = np.arange(-dopp_hz, dopp_hz + 1, dopp_step)
    best_pk = -1.0
    best = (0.0, 0)
    best_floor = 1.0
    for fd in dopps:
        acc = np.zeros(npp)
        for k in range(nc):
            blk = seg[k * npp:(k + 1) * npp]
            ph = np.exp(-1j * 2 * np.pi * (center_hz + fd) *
                        (np.arange(k * npp, (k + 1) * npp)) / fs)
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
