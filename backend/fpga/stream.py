"""Command-word stream for the FPGA engine ('gnss' TX format).

A band plan becomes a stream of records (``cmdfmt``): one CFG record, then one
BLOCK record per 65536 samples. Records are packed as uint32 words and grouped
into DMA buffers. The buffers are written non-cyclic to the DAC DMA (command
mode): the engine pops words from its FIFO behind the DMA, so a cyclic buffer
would replay old commands. ``DmaBuffer.cyclic`` is always False.

Channel capacity: ``select.nch_max(fs)`` bounds the channel count; surplus
satellites are dropped lowest elevation first (``select.select_svs``). If no
channel fits at all, or trimming is switched off, ``GnssPlanError`` is raised.
"""
from __future__ import annotations

import dataclasses
import math
from typing import Iterable, Iterator

import numpy as np

from backend.fpga import cmdfmt, select
from backend.fpga import widths as W
from backend.fpga.cmdgen import generate_blocks

MIN_FS = 2.05e6             # a block spans at most 32 code periods (sign_bits)
F_ENG = select.F_ENG
GROUP_BLOCKS = 8            # block records per DMA buffer
_COEF_LIMIT = (1 << (W.COEF_BITS - 1)) - 1
_CBOC_PEAK = 0.95346258924559231 + 0.30151134457776363   # A + B


class GnssPlanError(Exception):
    """The plan cannot be served by the engine."""


@dataclasses.dataclass(frozen=True)
class DmaBuffer:
    """One DAC DMA buffer: whole command records as uint32 words."""
    words: np.ndarray               # dtype '<u4'
    first_block: int | None         # None for the CFG buffer
    n_blocks: int
    cyclic: bool = False

    @property
    def nbytes(self) -> int:
        return int(self.words.nbytes)

    def tobytes(self) -> bytes:
        return self.words.tobytes()


def _words(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype="<u4").copy()


def out_lsb_for(level_dbfs: float | None, svs, cap: bool = True) -> float:
    """Engine output scale (``cmdgen`` out_lsb) for a composite RMS of
    ``level_dbfs`` below 16-bit full scale (each channel is 0.25*out_lsb/sqrt(n)
    at unit gain, so the composite is about 0.25*out_lsb), capped so every
    18-bit coefficient fits (spec 4.2: level cap on the host)."""
    full = 32767.0
    lsb = full if level_dbfs is None else 4.0 * full * 10.0 ** (level_dbfs / 20.0)
    lsb = min(lsb, full)
    if cap and len(svs):
        gmax = max(float(sv.gain) for sv in svs)
        if any(int(sv.cboc) and sv.sub_carrier_hz > 0.0 for sv in svs):
            gmax *= _CBOC_PEAK
        if gmax > 0.0:
            ceil_ = 0.995 * _COEF_LIMIT / (gmax * 0.25 / math.sqrt(len(svs))
                                           * (1 << W.COEF_FRAC_BITS))
            lsb = min(lsb, ceil_)
    return lsb


def plan_capacity(band, fs: float | None = None, *, trim: bool = True,
                  min_per_system: int = 3) -> select.ChannelPlan:
    """Check ``band`` against the engine limit; raise GnssPlanError if it
    cannot be served (and with ``trim=False`` if satellites would be dropped)."""
    fs = float(band.fs if fs is None else fs)
    if fs < MIN_FS:
        raise GnssPlanError(f"sample rate {fs / 1e6:g} Msps is below the "
                            f"{MIN_FS / 1e6:g} Msps minimum of the engine")
    if abs(fs - round(fs)) > 1e-6 or not 0 < round(fs) < 2**32:
        raise GnssPlanError(f"sample rate {fs!r} is not an integer Hz in uint32")
    limit = select.nch_max(fs)
    if limit < 1:
        raise GnssPlanError(f"engine serves no channel at {fs / 1e6:g} Msps "
                            f"(f_eng {F_ENG / 1e6:g} MHz)")
    if not trim and len(band.svs) > limit:
        raise GnssPlanError(
            f"plan needs {len(band.svs)} channels but the engine serves at most "
            f"{limit} at {fs / 1e6:g} Msps (lower the rate or enable trimming)")
    cplan = select.plan_channels(band, fs, F_ENG, min_per_system)
    if cplan.nch < 1:
        raise GnssPlanError("no channel left after elevation-based selection")
    return cplan


class CommandStream:
    """Iterator of ``DmaBuffer`` for one band plan.

    ``cfg=True`` emits the CFG record first (in its own buffer, so a start-up
    prefill can be exactly 'cfg + a few blocks'). ``first_block`` labels the
    absolute index of plan block 0 (continuing sessions use the running count).
    ``cfg_nch``: None = channels in use, "max" = ``nch_max(fs)``, or an int.
    """

    def __init__(self, band, *, first_block: int = 0, n_blocks: int | None = None,
                 level_dbfs: float | None = -15.0, cfg: bool = True,
                 trim: bool = True, group_blocks: int = GROUP_BLOCKS,
                 shift: int = 0, min_per_system: int = 3,
                 cfg_nch: int | str | None = None):
        if group_blocks < 1:
            raise ValueError("group_blocks must be >= 1")
        fs = float(band.fs)
        self.channel_plan = plan_capacity(band, fs, trim=trim,
                                          min_per_system=min_per_system)
        self.fs = fs
        self.nch = self.channel_plan.nch
        self.dropped = self.channel_plan.dropped
        self.out_lsb = out_lsb_for(level_dbfs, self.channel_plan.kept)
        self._band = band             # keep SvSpec buffers alive
        self._first = int(first_block)
        self._cfg = cfg
        self._group = int(group_blocks)
        self._shift = int(shift)
        # CFG nch fixes the engine's clocks per sample for the whole run
        # (gs_seq: tc = ceil(nch/P)). A live session whose satellite count
        # changes pins it to the capacity limit: "max".
        if cfg_nch == "max":
            cfg_nch = self.channel_plan.nch_max
        self._cfg_nch = self.nch if cfg_nch is None else int(cfg_nch)
        if self._cfg_nch < self.nch or self._cfg_nch > self.channel_plan.nch_max:
            raise GnssPlanError(f"cfg nch {self._cfg_nch} outside "
                                f"[{self.nch}, {self.channel_plan.nch_max}]")
        self._gen = generate_blocks(band, first_block=first_block,
                                    n_blocks=n_blocks, out_lsb=self.out_lsb,
                                    limit_fs=fs, min_per_system=min_per_system)
        self.total_blocks = min(int(band.total_samples) // int(band.block_samples),
                                n_blocks if n_blocks is not None else 1 << 62)

    def __iter__(self) -> Iterator[DmaBuffer]:
        if self._cfg:
            raw = cmdfmt.pack_cfg(cmdfmt.CfgCommand(
                self._cfg_nch, int(round(self.fs)), self._shift, self._first))
            yield DmaBuffer(_words(raw), None, 0)
        parts, idxs = [], []
        for idx, chans in self._gen:
            parts.append(cmdfmt.pack_block(idx, chans))
            idxs.append(idx)
            if len(parts) == self._group:
                yield DmaBuffer(_words(b"".join(parts)), idxs[0], len(parts))
                parts, idxs = [], []
        if parts:
            yield DmaBuffer(_words(b"".join(parts)), idxs[0], len(parts))


def build_stream(band, **kw) -> CommandStream:
    return CommandStream(band, **kw)


def concat_words(buffers: Iterable[DmaBuffer]) -> bytes:
    return b"".join(b.tobytes() for b in buffers)
