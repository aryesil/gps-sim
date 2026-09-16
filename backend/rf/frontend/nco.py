"""Phase-continuous complex baseband mixer for the LO-offset RF frontend.

Tracks an exact integer sample counter (never a rotating phase increment
accumulated call-over-call, which would drift under repeated float
rounding). The counter is reduced modulo one full phase period before it is
ever handed to float64 arithmetic -- the period is exact when freq_hz and
sample_rate_hz are whole-number Hz (the normal case for a TX LO offset and
sample rate), so the float64 index driving np.exp() stays small no matter
how long a session runs: `phase[k] = 2*pi*freq_hz/sample_rate_hz *
((n0 + k) mod period)`, with `n0` the exact sample count seen so far.

The relative rotor for a chunk length is cached across calls of the same
length (the common case -- transmit.py's stream() calls mix() with a fixed
chunk_samples every time): np.exp() runs once per distinct chunk length,
not once per call, and each mix() call is then one scalar exp() plus one
array multiply.
"""
from __future__ import annotations

import math

import numpy as np


class NCOMixer:
    def __init__(self, freq_hz: float, sample_rate_hz: float):
        self._freq_hz = float(freq_hz)
        self._sample_rate_hz = float(sample_rate_hz)
        self._phase_step = 2.0 * np.pi * self._freq_hz / self._sample_rate_hz
        self._n = 0  # exact integer sample count seen so far
        freq_i = round(self._freq_hz)
        rate_i = round(self._sample_rate_hz)
        g = math.gcd(abs(freq_i), rate_i) if rate_i else 0
        self._period = (rate_i // g) if g else (rate_i or 1)
        self._cached_len: int | None = None
        self._cached_rotor: np.ndarray | None = None  # complex64, phase_step*k for k in 0..len-1

    def _relative_rotor(self, n: int) -> np.ndarray:
        if self._cached_len != n:
            k = np.arange(n, dtype=np.float64)
            self._cached_rotor = np.exp(1j * self._phase_step * k).astype(np.complex64)
            self._cached_len = n
        return self._cached_rotor

    def mix(self, chunk: np.ndarray) -> np.ndarray:
        n = len(chunk)
        n0 = self._n % self._period
        origin = np.complex64(np.exp(1j * self._phase_step * n0))
        rotor = self._relative_rotor(n) * origin
        self._n += n
        return (np.asarray(chunk) * rotor).astype(chunk.dtype)
