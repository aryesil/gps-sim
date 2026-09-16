"""Phase-continuous complex baseband mixer for the LO-offset RF frontend.

Recomputes phase from an exact Python `int` sample counter each call
(never accumulates a rotating phase increment across calls), so float
precision does not drift over an arbitrarily long live session or file
replay: `phase[k] = 2*pi*freq_hz/sample_rate_hz * (n0 + k)`, with `n0` the
exact total sample count seen so far.
"""
from __future__ import annotations

import numpy as np


class NCOMixer:
    def __init__(self, freq_hz: float, sample_rate_hz: float):
        self._freq_hz = float(freq_hz)
        self._sample_rate_hz = float(sample_rate_hz)
        self._n = 0  # exact integer sample count seen so far

    def mix(self, chunk: np.ndarray) -> np.ndarray:
        n = len(chunk)
        idx = np.arange(self._n, self._n + n, dtype=np.float64)
        phase = (2.0 * np.pi * self._freq_hz / self._sample_rate_hz) * idx
        self._n += n
        rotor = np.exp(1j * phase)
        return (np.asarray(chunk) * rotor).astype(chunk.dtype)
