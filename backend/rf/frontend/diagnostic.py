"""Baseband CW tone generator for RF diagnostics -- GPS IQ generator is not
involved at all. Yields complex64 chunks indefinitely; the caller's
TxSession cancel event (same as any transmit.stream() chunk_source) is what
stops consumption. `tone_hz=0.0` (the default) is a pure baseband DC
carrier: whatever `baseband_offset_hz` the RF-frontend plan computed places
it at the requested target RF exactly like any other TX content, via the
same NCOMixer path (backend/rf/transmit.py's stream()) -- no separate
frequency-placement logic here.
"""
from __future__ import annotations

from collections.abc import Iterator

import numpy as np


def cw_chunk_source(sample_rate_hz: float, chunk_samples: int = 65536,
                     tone_hz: float = 0.0) -> Iterator[np.ndarray]:
    n = 0
    while True:
        if tone_hz:
            idx = np.arange(n, n + chunk_samples, dtype=np.float64)
            phase = (2.0 * np.pi * tone_hz / sample_rate_hz) * idx
            chunk = np.exp(1j * phase).astype(np.complex64)
        else:
            chunk = np.ones(chunk_samples, dtype=np.complex64)
        n += chunk_samples
        yield chunk
