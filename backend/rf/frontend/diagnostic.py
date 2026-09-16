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

from backend.rf.frontend.nco import NCOMixer


def cw_chunk_source(sample_rate_hz: float, chunk_samples: int = 65536,
                     tone_hz: float = 0.0) -> Iterator[np.ndarray]:
    mixer = NCOMixer(tone_hz, sample_rate_hz) if tone_hz else None
    carrier = np.ones(chunk_samples, dtype=np.complex64)
    while True:
        yield mixer.mix(carrier) if mixer is not None else carrier
