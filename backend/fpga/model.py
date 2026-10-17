"""Bit-exact (integer) reference model of the FPGA GNSS synthesis engine.

One block of BLOCK samples is a pure function of its ChannelBlock records
(parameters are absolute per block), so the model keeps no sample state, only
the block sequencing (``next_block``) and counters.

Sign convention (matches cmdgen and the C++ oracle in the tests): the period
sign is +1 when bit ((sign_idx0 + sp // (code_len << 40)) mod 32) of
sign_bits is 1.
"""
from __future__ import annotations

import numpy as np

from backend.fpga import roms
from backend.fpga import widths as W

_OUT_SHIFT = W.TABLE_BITS - 1 + W.COEF_FRAC_BITS


def _build_table():
    cs = np.array([roms.table_value(i) for i in range(1 << W.LUT_BITS)], np.int64)
    return cs[:, 0].copy(), cs[:, 1].copy()


_COS, _SIN = _build_table()


class EngineModel:
    def __init__(self, fs: float, nch_max: int = W.MAX_CH):
        self.fs = fs
        self.nch_max = nch_max
        self.next_block = None
        self.underruns = 0
        self.clipped = 0
        self.dropped_late = 0
        self._rom = {}

    def start(self, block_idx: int) -> None:
        self.next_block = int(block_idx)

    def _chips(self, kind, prn):
        key = (kind, prn)
        if key not in self._rom:
            w = roms.code_rom_words(kind, prn)
            bits = (w[:, None] >> np.arange(32, dtype=np.uint32)) & 1
            self._rom[key] = bits.reshape(-1).astype(np.int64) * 2 - 1
        return self._rom[key]

    def _process(self, chans) -> np.ndarray:
        if len(chans) > self.nch_max:
            raise ValueError(f"{len(chans)} channels > nch_max {self.nch_max}")
        n = W.BLOCK
        k = np.arange(n, dtype=np.int64)
        acc_i = np.zeros(n, np.int64)
        acc_q = np.zeros(n, np.int64)
        for c in chans:
            if not c.valid:
                continue
            lim = c.code_len << W.CODE_FRAC_BITS
            ph = (c.carr_phase0 + k * (c.carr_inc & 0xFFFFFFFF)) & 0xFFFFFFFF
            idx = ((ph + W.LUT_ROUND) >> 20) & 0xFFF
            cp = (c.code_phase0 + k * c.code_inc) % lim
            chip = self._chips(c.kind, c.prn)[cp >> W.CODE_FRAC_BITS]
            sp = c.sign_phase0 + k * c.code_inc
            per = (c.sign_idx0 + sp // lim) % W.SIGN_BITS
            sgn = ((c.sign_bits >> per) & 1) * 2 - 1
            if c.boc:
                s11 = 1 - 2 * ((cp >> (W.CODE_FRAC_BITS - 1)) & 1)
                if c.cboc:
                    s61 = 1 - 2 * (((12 * cp) >> W.CODE_FRAC_BITS) & 1)
                    mag = np.where(c.cboc * s11 * s61 > 0, c.coef[1], c.coef[2])
                else:
                    mag = c.coef[0]
                coef = mag * s11
            else:
                coef = np.full(n, c.coef[0], np.int64)
            a = coef * chip * sgn
            acc_i += a * _COS[idx]
            acc_q += a * _SIN[idx]
        half = 1 << (_OUT_SHIFT - 1)
        out = np.empty((n, 2), np.int16)
        for j, acc in enumerate((acc_i, acc_q)):
            v = (acc + half) >> _OUT_SHIFT
            sat = (v > 32767) | (v < -32768)
            self.clipped += int(sat.sum())
            out[:, j] = np.clip(v, -32768, 32767).astype(np.int16)
        return out

    def feed_block(self, block_idx: int, chans) -> np.ndarray:
        zero = np.zeros((W.BLOCK, 2), np.int16)
        if self.next_block is None:
            self.next_block = int(block_idx)
        if chans is None:
            self.underruns += 1
            self.next_block += 1
            return zero
        if block_idx < self.next_block:
            self.dropped_late += 1
            return np.zeros((0, 2), np.int16)
        parts = []
        while self.next_block < block_idx:
            self.underruns += 1
            self.next_block += 1
            parts.append(zero)
        parts.append(self._process(chans))
        self.next_block += 1
        return parts[0] if len(parts) == 1 else np.concatenate(parts)
