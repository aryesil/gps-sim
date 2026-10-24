# SPDX-License-Identifier: MIT
"""GPS L1 C/A channel tracker: FLL pull-in, then PLL (Costas) with carrier-aided DLL.

One integration spans exactly one code period (about 1 ms). Samples are consumed through
``push``/``step``; every completed epoch yields a measurement record with the absolute
sample index of the epoch start, so all timing is in the receiver's own sample clock.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import CHIP_HZ, CODE_LEN, L1_HZ
from .ca_code import ca_code

E_L_HALF = 0.25          # early/late offset in chips (spacing 0.5 chip)
NOISE_OFFSET = 3.0       # chips, noise-only correlator
FLL_GAIN = 0.05          # per-epoch FLL gain; 1 ms atan discriminator noise is tens of Hz at 44 dB-Hz


@dataclass
class Epoch:
    sample_index: int        # absolute index of the first sample of this integration
    n: int                   # samples integrated
    carrier_cycles: float    # accumulated NCO phase + residual phase (cycles), continuous
    freq_hz: float           # NCO baseband frequency used in this epoch
    code_phase: float        # chips at epoch start
    i_p: float
    q_p: float
    noise_p: float
    locked: bool
    cn0_dbhz: float


@dataclass
class Tracker:
    fs: float
    prn: int
    doppler_hz: float                   # initial baseband frequency estimate
    start_sample: int                   # absolute index where a code epoch starts
    pll_bw_hz: float = 12.0
    dll_bw_hz: float = 1.0
    fll_epochs: int = 600
    epochs: list = field(default_factory=list)

    def __post_init__(self):
        self._buf = np.zeros(0, complex)
        self._buf0 = int(self.start_sample)    # absolute index of _buf[0]
        self._code = ca_code(self.prn)
        self._f = float(self.doppler_hz)       # NCO frequency (Hz)
        self._f_int = float(self.doppler_hz)   # loop integrator state
        self._phase = 0.0                      # NCO phase, cycles (continuous)
        self._code_phase = 0.0                 # chips at start of the next epoch
        self._code_rate = CHIP_HZ
        self._dll_int = 0.0
        self._prev = None
        self._k = 0
        self._ps = []
        self._ns = []
        self._cn0 = 0.0

    def push(self, x):
        self._buf = np.concatenate([self._buf, np.asarray(x, complex)])

    def _block_len(self) -> int:
        return int(np.ceil((CODE_LEN - self._code_phase) * self.fs / self._code_rate))

    def step(self):
        out = []
        while True:
            n = self._block_len()
            if len(self._buf) < n:
                break
            out.append(self._process(self._buf[:n], n))
            self._buf = self._buf[n:]
            self._buf0 += n
        self.epochs.extend(out)
        return out

    def _replica(self, n, chips0, rate, offset):
        c = chips0 + np.arange(n) * rate / self.fs + offset
        return self._code[np.floor(c).astype(np.int64) % CODE_LEN]

    def _process(self, seg, n):
        fs = self.fs
        T = n / fs
        wipe = np.exp(-2j * np.pi * (self._phase + self._f * np.arange(n) / fs))
        x = seg * wipe
        cp, rate = self._code_phase, self._code_rate
        P = np.sum(x * self._replica(n, cp, rate, 0.0))
        E = np.sum(x * self._replica(n, cp, rate, +E_L_HALF))
        L = np.sum(x * self._replica(n, cp, rate, -E_L_HALF))
        Nz = np.sum(x * self._replica(n, cp, rate, NOISE_OFFSET))
        pre_phase = self._phase
        idx0 = self._buf0

        phase_err = np.arctan(P.imag / P.real) / (2 * np.pi) if P.real != 0 else 0.0   # cycles
        if self._prev is not None:
            cross = self._prev.real * P.imag - self._prev.imag * P.real
            dot = self._prev.real * P.real + self._prev.imag * P.imag
            f_err = np.arctan(cross / dot) / (2 * np.pi * T) if dot != 0 else 0.0
        else:
            f_err = 0.0
        eabs, labs = abs(E), abs(L)
        code_err = 0.75 * (eabs - labs) / (eabs + labs) if (eabs + labs) > 0 else 0.0   # chips

        if self._k < self.fll_epochs:
            self._f_int += FLL_GAIN * f_err
            self._f = self._f_int
        else:
            w0 = self.pll_bw_hz / 0.53
            self._f_int += w0 * w0 * T * phase_err
            self._f = self._f_int + 1.414 * w0 * phase_err
        wd = self.dll_bw_hz / 0.53
        self._dll_int += wd * wd * T * code_err
        dll_freq = self._dll_int + 1.414 * wd * code_err
        self._code_rate = CHIP_HZ * (1.0 + self._f / L1_HZ) + dll_freq

        self._phase = pre_phase + self._f * n / fs
        self._code_phase = cp + n * rate / fs - CODE_LEN
        self._prev = P

        self._ps.append(abs(P) ** 2)
        self._ns.append(abs(Nz) ** 2)
        if len(self._ps) >= 100:
            sp, npw = np.mean(self._ps), np.mean(self._ns)
            rho = max(sp - npw, 1e-12) / max(npw, 1e-12)
            self._cn0 = float(10 * np.log10(rho / T))
            self._ps.clear()
            self._ns.clear()
        locked = self._k >= self.fll_epochs and abs(P.imag) < abs(P.real) * 0.6 and self._cn0 > 25.0
        self._k += 1
        total = pre_phase + phase_err
        return Epoch(idx0, n, float(total), float(self._f), float(cp), float(P.real), float(P.imag),
                     float(abs(Nz) ** 2), bool(locked), self._cn0)
