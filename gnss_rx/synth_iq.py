# SPDX-License-Identifier: MIT
"""Streaming synthetic L1 C/A baseband generator for receiver tests.

Output is complex baseband centred on the nominal L1 frequency of the receiver LO. Every
satellite carries a Doppler (Hz at L1, linear rate), a common ``clock_offset_hz`` (the
receiver oscillator error expressed at L1) and random 50 Hz data bits.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import CHIP_HZ, CODE_LEN, L1_HZ
from .ca_code import ca_code


@dataclass
class SvSpec:
    prn: int
    doppler_hz: float
    cn0_dbhz: float = 42.0
    doppler_rate_hz_s: float = 0.0
    code_phase_chips: float = 0.0
    carrier_phase_cycles: float = 0.0


class SynthIQ:
    def __init__(self, fs: float, svs: list[SvSpec], clock_offset_hz: float = 0.0,
                 clock_drift_hz_s: float = 0.0, seed: int = 1, clock_y: float = 0.0):
        self.fs = float(fs)
        self.svs = svs
        self.clock_offset_hz = clock_offset_hz
        self.clock_drift_hz_s = clock_drift_hz_s
        self.clock_y = clock_y          # oscillator fractional error (fast = positive): LO offset and time dilation
        self.rng = np.random.default_rng(seed)
        self.n = 0                      # absolute sample index of the next sample
        self._bits = {s.prn: self.rng.choice([-1.0, 1.0], size=4096) for s in svs}

    def read(self, count: int) -> np.ndarray:
        fs = self.fs
        t_rx = (self.n + np.arange(count)) / fs
        # a fast oscillator makes receiver seconds shorter: physical time = receiver time / (1 + y)
        t = t_rx / (1.0 + self.clock_y)
        out = (self.rng.standard_normal(count) + 1j * self.rng.standard_normal(count)) / np.sqrt(2.0)
        for s in self.svs:
            f0 = s.doppler_hz + self.clock_offset_hz - self.clock_y * L1_HZ
            # integral of f(t) = f0 + (rate_sv + drift) t
            rate = s.doppler_rate_hz_s + self.clock_drift_hz_s
            cyc = s.carrier_phase_cycles + f0 * t + 0.5 * rate * t * t
            # code phase advances at the chip rate scaled by the carrier Doppler (code-carrier coherence)
            code_rate_scale = 1.0 + (s.doppler_hz + self.clock_offset_hz) / L1_HZ
            chips = s.code_phase_chips + CHIP_HZ * code_rate_scale * t
            idx = np.floor(chips).astype(np.int64) % CODE_LEN
            bit_idx = np.floor(t / 0.020).astype(np.int64) % 4096
            amp = np.sqrt(10.0 ** (s.cn0_dbhz / 10.0) / fs)
            out += amp * ca_code(s.prn)[idx] * self._bits[s.prn][bit_idx] * np.exp(2j * np.pi * cyc)
        self.n += count
        return out
