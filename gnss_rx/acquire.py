# SPDX-License-Identifier: MIT
"""Parallel code-phase search (FFT) acquisition for GPS L1 C/A."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import CHIP_HZ, CODE_LEN
from .ca_code import ca_code


@dataclass
class AcqResult:
    prn: int
    doppler_hz: float
    code_phase_samples: int      # offset of the first code epoch inside the supplied block
    metric: float                # peak / best peak outside +-1.5 chips
    peak: float


def _replica(prn: int, fs: float, n: int) -> np.ndarray:
    idx = np.floor(np.arange(n) * CHIP_HZ / fs).astype(np.int64) % CODE_LEN
    return ca_code(prn)[idx]


def acquire(iq: np.ndarray, fs: float, prn: int, *, doppler_max_hz: float = 10000.0,
            doppler_step_hz: float = 250.0, coherent_ms: int = 1, n_noncoh: int = 10) -> AcqResult:
    """Search one PRN. ``iq`` must hold at least ``coherent_ms * n_noncoh`` milliseconds."""
    n1 = int(round(fs * 1e-3))
    n = n1 * coherent_ms
    need = n * n_noncoh
    if len(iq) < need:
        raise ValueError(f"need {need} samples, got {len(iq)}")
    # replica spans one code period (1 ms); for coherent_ms > 1 it is repeated
    rep = np.tile(_replica(prn, fs, n1), coherent_ms)
    R = np.conj(np.fft.fft(rep))
    t = np.arange(n) / fs
    dopplers = np.arange(-doppler_max_hz, doppler_max_hz + 1e-9, doppler_step_hz)
    acc = np.zeros((len(dopplers), n))
    blocks = np.asarray(iq[:need]).reshape(n_noncoh, n)
    for i, fd in enumerate(dopplers):
        w = np.exp(-2j * np.pi * fd * t)
        for b in range(n_noncoh):
            c = np.fft.ifft(np.fft.fft(blocks[b] * w) * R)
            acc[i] += np.abs(c) ** 2
    i_pk, k_pk = np.unravel_index(np.argmax(acc), acc.shape)
    peak = acc[i_pk, k_pk]
    # best value at the peak Doppler bin outside +-1.5 chips of the peak lag
    guard = int(np.ceil(1.5 * fs / CHIP_HZ))
    lag = np.arange(n)
    dist = np.minimum((lag - k_pk) % n, (k_pk - lag) % n)
    row = acc[i_pk].copy()
    row[dist <= guard] = 0.0
    second = row.max()
    # circular cross-correlation peak at lag k: x[m + k] == replica[m], i.e. the signal's code
    # epoch sits at sample k of the block.
    start = int(k_pk % n1)
    return AcqResult(prn, float(dopplers[i_pk]), start, float(peak / max(second, 1e-30)), float(peak))


def acquire_weak_many(iq: np.ndarray, fs: float, prns, *, doppler_max_hz: float = 12000.0,
                      coherent_ms: int = 10, n_noncoh: int = 10, pad: int = 4) -> list[AcqResult]:
    """Long integration search for weak signals (unpowered or passive antenna, indoor).

    The signal is correlated with the code replica once per millisecond; a coarse Doppler grid
    (1 kHz) removes most of the carrier and an FFT across ``coherent_ms`` consecutive
    milliseconds resolves the remaining +-500 Hz. ``coherent_ms`` should stay below the 20 ms
    navigation bit length; ``n_noncoh`` such segments are summed in power. The carrier
    wipe-off and the block FFT are shared by all PRNs.
    """
    prns = list(prns)
    n1 = int(round(fs * 1e-3))
    nb = coherent_ms * n_noncoh
    if len(iq) < nb * n1:
        raise ValueError(f"need {nb * n1} samples, got {len(iq)}")
    R = {p: np.conj(np.fft.fft(_replica(p, fs, n1))) for p in prns}
    blocks = np.asarray(iq[:nb * n1]).reshape(nb, n1)
    t = np.arange(nb * n1).reshape(nb, n1) / fs
    coarse = np.arange(-doppler_max_hz, doppler_max_hz + 1e-9, 1000.0)
    nfft = coherent_ms * pad
    f_res = np.fft.fftfreq(nfft, 1e-3)                                   # Hz, +-500
    best = {p: (-1.0, None, None) for p in prns}                         # peak, acc slice, coarse index
    for i, fd in enumerate(coarse):
        X = np.fft.fft(blocks * np.exp(-2j * np.pi * fd * t), axis=1)
        for p in prns:
            c = np.fft.ifft(X * R[p], axis=1).reshape(n_noncoh, coherent_ms, n1)
            acc = (np.abs(np.fft.fft(c, n=nfft, axis=1)) ** 2).sum(axis=0)
            pk = acc.max()
            if pk > best[p][0]:
                best[p] = (pk, acc, i)
    guard = int(np.ceil(1.5 * fs / CHIP_HZ))
    lag = np.arange(n1)
    out = []
    for p in prns:
        pk, acc, i = best[p]
        j_pk, k_pk = np.unravel_index(np.argmax(acc), acc.shape)
        dist = np.minimum((lag - k_pk) % n1, (k_pk - lag) % n1)
        second = acc[:, dist > guard].max()                              # best value away from the peak lag
        out.append(AcqResult(p, float(coarse[i] + f_res[j_pk]), int(k_pk % n1),
                             float(pk / max(second, 1e-30)), float(pk)))
    return out


def acquire_weak(iq: np.ndarray, fs: float, prn: int, **kw) -> AcqResult:
    return acquire_weak_many(iq, fs, [prn], **kw)[0]
