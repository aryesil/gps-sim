"""Shared test helpers.

``synth_iq`` builds a deterministic, hardware-free GPS-like L1 C/A IQ
buffer: a handful of PRNs, each a C/A-coded BPSK carrier at a chosen
Doppler and code phase, summed and (optionally) quantised to an
interleaved integer buffer. No RNG unless a noise level is asked for,
and then it is seeded. It is good enough to acquire and to exercise the
impairment / integrity layers; it is not a full navigation signal (no
LNAV data bits, no atmospheric or clock effects).
"""
from __future__ import annotations

import numpy as np
import pytest

from backend import inspector

_CA_CHIP_HZ = 1_023_000.0


def _make_iq(prns=(1, 7, 13, 21), sample_rate=2.6e6, duration_s=0.05,
             dopplers=None, code_phases_chips=None, amp=20.0,
             noise_rms=1.5, seed=0, sample_format="int16", quantize=True):
    n = int(round(sample_rate * duration_s))
    t = np.arange(n) / sample_rate
    sig = np.zeros(n, dtype=np.complex128)
    rng = np.random.default_rng(seed)
    # every planted PRN gets a distinct non-zero Doppler and a random
    # carrier phase, so no single satellite dominates I or Q.
    dopplers = dopplers or {p: (600.0 + 850.0 * k) * (1.0 if k % 2 else -1.0)
                            for k, p in enumerate(prns)}
    code_phases_chips = code_phases_chips or {p: (k * 137.0 + 40.0) % 1023
                                             for k, p in enumerate(prns)}
    phases = {p: float(rng.uniform(0, 2 * np.pi)) for p in prns}
    for p in prns:
        code = inspector.ca_code(p).astype(np.float64)
        chips = (t * _CA_CHIP_HZ + code_phases_chips[p]).astype(np.int64) % 1023
        seq = code[chips]
        sig += amp * seq * np.exp(1j * (2 * np.pi * dopplers[p] * t + phases[p]))
    if noise_rms:
        sig = sig + rng.normal(0, noise_rms, n) + 1j * rng.normal(0, noise_rms, n)
    if not quantize:
        return sig.astype(np.complex64), {"n": n, "dopplers": dopplers,
                                          "code_phases_chips": code_phases_chips}
    fs = 127.0 if sample_format == "int8" else 32767.0
    peak = np.max(np.abs(np.concatenate([sig.real, sig.imag]))) or 1.0
    scale = (fs * 0.7) / peak
    dtype = np.int8 if sample_format == "int8" else np.int16
    inter = np.empty(2 * n, dtype=dtype)
    inter[0::2] = np.clip(np.round(sig.real * scale), -fs, fs)
    inter[1::2] = np.clip(np.round(sig.imag * scale), -fs, fs)
    return inter, {"n": n, "dopplers": dopplers,
                   "code_phases_chips": code_phases_chips, "scale": scale}


@pytest.fixture
def synth_iq():
    return _make_iq
