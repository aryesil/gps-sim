# SPDX-License-Identifier: MIT
"""GPS L1 C/A Gold code generator (IS-GPS-200, G2 phase selection taps)."""
from __future__ import annotations

import numpy as np

# (tap1, tap2) of the G2 register for PRN 1..32, IS-GPS-200 Table 3-Ia
_G2_TAPS = [
    (2, 6), (3, 7), (4, 8), (5, 9), (1, 9), (2, 10), (1, 8), (2, 9),
    (3, 10), (2, 3), (3, 4), (5, 6), (6, 7), (7, 8), (8, 9), (9, 10),
    (1, 4), (2, 5), (3, 6), (4, 7), (5, 8), (6, 9), (1, 3), (4, 6),
    (5, 7), (6, 8), (7, 9), (8, 10), (1, 6), (2, 7), (3, 8), (4, 9),
]

_cache: dict[int, np.ndarray] = {}


def ca_code(prn: int) -> np.ndarray:
    """Return the 1023 chips of PRN ``prn`` (1..32) as float +-1, chip bit 1 mapped to -1."""
    if not 1 <= prn <= 32:
        raise ValueError(f"PRN must be 1..32, got {prn}")
    if prn in _cache:
        return _cache[prn]
    g1 = [1] * 10
    g2 = [1] * 10
    t1, t2 = _G2_TAPS[prn - 1]
    out = np.empty(1023)
    for i in range(1023):
        bit = g1[9] ^ g2[t1 - 1] ^ g2[t2 - 1]
        out[i] = -1.0 if bit else 1.0
        f1 = g1[2] ^ g1[9]
        f2 = g2[1] ^ g2[2] ^ g2[5] ^ g2[7] ^ g2[8] ^ g2[9]
        g1 = [f1] + g1[:9]
        g2 = [f2] + g2[:9]
    out.setflags(write=False)
    _cache[prn] = out
    return out
