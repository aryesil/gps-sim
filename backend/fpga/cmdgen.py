"""Block command generator: engine ``BandPlan`` -> per-block channel records.

Everything is derived from the native mixer's own semantics
(``backend/synth/native/mixer.cpp`` ``prepare_sv`` / ``mix_sv_into``), evaluated
at the first sample of each block with the same double-precision formulas, then
reduced to the fixed-point wire units of ``cmdfmt``.

Channel numbering: ``ch`` is the index in ``band.svs``.  Plain GPS C/A,
Galileo E1B/E1C and BeiDou B1I only.

Per-channel block parameters
    carrier   word = round((f/fs mod 1) * 2^32) as int32 (two's complement,
              exactly ``Nco::set_freq``); phase0 = uint32(frac(phase/2pi)*2^32)
              with phase = knot phase + 2*pi*f*(t0 - knot_t0) (``prepare_sv``).
    code      increment = rate/fs in chips with 40 fractional bits; phase0 =
              (code phase of the block's first sample) mod code_len, same units.
              Trajectory channels: ``traj_code_phase[j] + rate_j*(t0-seg_t0)``;
              Phase-1 channels (traj_nknots == 0): ``eff + rate*t0`` with
              ``eff = L - (code_phase0 mod L)`` and rate = chip rate + code
              Doppler.
    sign      see ``sign_bits_for_block``.
    gains     ``coef = round(gain * scale * 2^COEF_FRAC_BITS)`` with
              ``scale = 0.25 * out_lsb / sqrt(len(band.svs))`` (engine.cpp);
              CBOC channels also ``g*(A+B)`` and ``g*(A-B)``.

Sign-bit convention (documented once, used by ``sign_bits_for_block``)
    u0 = (code phase of the block's first sample) + tx_chips_offset, in chips.
    p0 = floor(u0 / code_len); sign_idx0 = 0 and bit i (bit 0 = LSB) is the sign
    of primary-code period p0 + i; bit 1 = multiply by +1, bit 0 = -1 (the ROM
    convention).  sign_phase0 = (u0 mod code_len) in chips, 40 fractional bits.
    The sign of period p is  sec[p mod sec_len] * nav_symbol(p // m)  where
    m = chips_per_symbol / code_len (integer for every v1 signal).
"""
from __future__ import annotations

import dataclasses
import math
from typing import Callable, Iterator

import numpy as np

from backend.fpga import cmdfmt, roms
from backend.fpga import widths as W
from backend.fpga.select import ChannelPlan, plan_channels  # noqa: F401

CBOC_A = 0.95346258924559231     # kCbocA, mixer.cpp
CBOC_B = 0.30151134457776363     # kCbocB, mixer.cpp
_SYS_GPS, _SYS_SBAS, _SYS_BEIDOU, _SYS_GALILEO = 0, 2, 3, 5   # engine._SYS_INT
_TWO_PI = 2.0 * math.pi
_U32 = 0xFFFFFFFF
_FRAC = 1 << W.CODE_FRAC_BITS
_NAV_DEFAULT_HZ = 50.0           # constants.hpp kNavBitHz


def _rnd(x: float) -> int:
    """Round half up (llround for the non-negative values used here)."""
    return int(math.floor(x + 0.5))


def _code_len(sv) -> int:
    return int(sv.code_len) if sv.code_len > 0 else 1023


def _chip_rate(sv) -> float:
    return float(sv.chip_rate_hz) if sv.chip_rate_hz > 0.0 else 1.023e6


def _check_supported(sv, ch: int) -> None:
    tag = f"channel {ch} (sys {sv.sys}, prn {sv.prn})"
    if sv.sys not in (_SYS_GPS, _SYS_SBAS, _SYS_GALILEO, _SYS_BEIDOU):
        raise NotImplementedError(
            f"{tag}: system {sv.sys} not supported by the FPGA command "
            "generator (GPS 0, SBAS 2, BeiDou 3, Galileo 5 only)")
    if sv.tx_time_valid == 0:
        raise NotImplementedError(
            f"{tag}: tx_time_valid == 0 (receiver-time nav) is not supported")
    if sv.fading.model != 0:
        raise NotImplementedError(f"{tag}: fading / gain knots not supported")


def _kind_of(sv) -> int:
    if sv.sys in (_SYS_GPS, _SYS_SBAS):
        return cmdfmt.KIND_CA
    if sv.sys == _SYS_BEIDOU:
        return cmdfmt.KIND_B1I
    if sv.nav_mode != 0:
        return cmdfmt.KIND_E1B
    return cmdfmt.KIND_E1C if sv.sec_len == 25 else cmdfmt.KIND_E1B


class _Static:
    """Block-independent per-channel data (views into the SvSpec buffers)."""

    def __init__(self, sv, ch: int):
        _check_supported(sv, ch)
        self.sv = sv
        self.ch = ch
        self.kind = _kind_of(sv)
        # PRN field of the command record = C/A ROM index (SBAS 120..150 -> 33..63)
        self.rom_prn = (roms.ca_index(int(sv.sys), int(sv.prn))
                        if self.kind == cmdfmt.KIND_CA else int(sv.prn))
        self.L = _code_len(sv)
        self.rate_nom = _chip_rate(sv)
        self.sec = (np.ctypeslib.as_array(sv.sec_code, (sv.sec_len,))
                    if sv.sec_len > 0 and sv.sec_code else None)
        has_nav = sv.nav_mode != 0 and sv.nav_nbits > 0 and bool(sv.nav_bits)
        self.nav = (np.ctypeslib.as_array(sv.nav_bits, (sv.nav_nbits,))
                    if has_nav else None)
        self.sym_per_period_inv = 1
        if self.nav is not None:
            nav_hz = (sv.nav_sym_rate_hz if sv.nav_sym_rate_hz > 0.0
                      else _NAV_DEFAULT_HZ)
            ratio = self.rate_nom / nav_hz / self.L      # periods per symbol
            m = round(ratio)
            if m < 1 or abs(ratio - m) > 1e-9:
                raise NotImplementedError(
                    f"channel {ch}: nav symbol is {ratio} code periods, "
                    "not a whole number")
            self.sym_per_period_inv = m


def _kinematics(sv, L: int, rate_nom: float, sample0: int, fs: float):
    """(carrier_hz, carrier_phase_rad_at_t0, code_rate, cp0) as prepare_sv."""
    dt = 1.0 / fs
    abs_t0 = sample0 * dt
    if sv.traj_nknots > 0:
        ks = int(sv.traj_knot_samples)
        j = sample0 // ks if ks else 0
        j = max(0, min(j, sv.traj_nknots - 1))
        seg_t0 = float(j * ks) / fs
        f = sv.traj_carr_freq[j]
        ph = sv.traj_carr_phase[j]
        rate = sv.traj_code_rate[j]
        cp0 = sv.traj_code_phase[j] + rate * (abs_t0 - seg_t0)
    else:
        seg_t0 = 0.0
        f = sv.carrier_freq_hz
        ph = sv.carrier_phase0_rad
        rate = rate_nom + sv.code_doppler_hz
        eff = math.fmod(sv.code_phase0_chips, float(L))
        if eff < 0:
            eff += L
        eff = L - eff
        if eff >= L:
            eff -= L
        cp0 = eff + rate * abs_t0
    return f, ph + _TWO_PI * f * (abs_t0 - seg_t0), rate, cp0


def _sign_state(st: _Static, cp0: float):
    """(p0, sign_phase0 fixed) for the code phase ``cp0`` of the first sample."""
    L = st.L
    u0 = cp0 + st.sv.tx_chips_offset
    p0 = math.floor(u0 / L)
    frac = u0 - p0 * L
    if frac < 0.0:
        frac += L
        p0 -= 1
    elif frac >= L:
        frac -= L
        p0 += 1
    fx = _rnd(frac * _FRAC)
    if fx >= L * _FRAC:
        fx -= L * _FRAC
        p0 += 1
    return int(p0), fx


def _bits(st: _Static, p0: int) -> int:
    p = p0 + np.arange(W.SIGN_BITS, dtype=np.int64)
    s = np.ones(W.SIGN_BITS, np.int64)
    if st.sec is not None:
        s = s * st.sec[p % len(st.sec)]
    if st.nav is not None:
        idx = (p // st.sym_per_period_inv) % len(st.nav)
        s = s * np.where(st.nav[idx] >= 0, 1, -1)
    bits = 0
    for i in np.flatnonzero(s > 0):
        bits |= 1 << int(i)
    return bits


def _sign_for(st: _Static, cp0: float, rate: float, fs: float,
              block_samples: int):
    p0, sp = _sign_state(st, cp0)
    inc = _rnd(rate / fs * _FRAC)
    end = sp + (block_samples - 1) * inc
    nper = end // (st.L * _FRAC) + 1
    if nper > W.SIGN_BITS:
        raise ValueError(
            f"channel {st.ch}: block spans {nper} code periods > "
            f"{W.SIGN_BITS} sign bits (sample rate too low for this block "
            "length)")
    return sp, _bits(st, p0)


def sign_bits_for_block(sv, block: int, fs: float, block_samples: int
                        ) -> tuple[int, int, int]:
    """(sign_idx0, sign_phase0, sign_bits) in cmdfmt wire units for ``block``.

    Convention: sign_idx0 = 0; bit i (LSB = bit 0) is the sign of primary-code
    period p0 + i, p0 = floor(u0 / code_len), u0 = code phase of the block's
    first sample + tx_chips_offset; 1 = multiply by +1, 0 = -1.  sign_phase0 is
    (u0 mod code_len) in chips with 40 fractional bits.  Raises ValueError if
    the block spans more than 32 code periods.
    """
    st = _Static(sv, 0)
    _, _, rate, cp0 = _kinematics(sv, st.L, st.rate_nom, block * block_samples,
                                  fs)
    sp, bits = _sign_for(st, cp0, rate, fs, block_samples)
    return 0, sp, bits


def _carr_words(f: float, ph: float, fs: float) -> tuple[int, int]:
    frac = f / fs
    frac -= math.floor(frac)
    inc = _rnd(frac * 4294967296.0) & _U32
    if inc >= 1 << 31:
        inc -= 1 << 32
    p = math.fmod(ph, _TWO_PI)
    cyc = p / _TWO_PI
    cyc -= math.floor(cyc)
    return inc, int(cyc * 4294967296.0) & _U32


def _coefs(g: float, scale: float, cboc: int) -> tuple[int, int, int]:
    k = scale * (1 << W.COEF_FRAC_BITS)
    c0 = _rnd(g * k)
    if cboc:
        c = (c0, _rnd(g * k * (CBOC_A + CBOC_B)), _rnd(g * k * (CBOC_A - CBOC_B)))
    else:
        c = (c0, c0, c0)
    lim = 1 << (W.COEF_BITS - 1)
    for v in c:
        if not -lim <= v < lim:
            raise ValueError(
                f"coefficient {v} does not fit signed {W.COEF_BITS} bits "
                f"(gain {g}, scale {scale})")
    return c


class _Stream:
    """Iterator wrapper carrying the ``ChannelPlan`` of a limited run."""

    def __init__(self, it, channel_plan):
        self._it = it
        self.channel_plan = channel_plan
        self.dropped = channel_plan.dropped

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)


def generate_blocks(band, first_block: int = 0, n_blocks: int | None = None,
                    out_lsb: float = 32767.0, *, sample_offset: int = 0,
                    active: Callable[[int, int], bool] | None = None,
                    limit_fs: float | None = None, f_eng: float = 250e6,
                    min_per_system: int = 3
                    ) -> Iterator[tuple[int, list[cmdfmt.ChannelBlock]]]:
    """Yield ``(first_block + j, [ChannelBlock, ...])`` for plan blocks
    j = 0 .. total_samples // block_samples - 1 (at most ``n_blocks``).

    Plan block j covers samples [j*B + sample_offset, (j+1)*B + sample_offset)
    of the plan; ``first_block`` only labels the absolute command index.
    ``sample_offset`` lets a plan that starts earlier than the first emitted
    block (on a whole code/data period, since a plan's code phase is relative
    to its start) serve a later block.  A trailing partial block is not emitted.  The
    optional keyword ``active(ch, j)`` lets a caller drop a channel from a
    block (satellite set/rise); a dropped channel simply has no record and
    nothing else moves, as every parameter is a function of the absolute
    sample index.  Keep ``band`` referenced while iterating (the SvSpec
    pointers point into ``band.keep_alive``).  Unsupported channels raise
    NotImplementedError immediately, not on the first ``next``.

    ``limit_fs`` (opt-in): apply ``select.select_svs(band, limit_fs, f_eng)``
    and generate only the kept SVs, renumbered ``ch = 0..n-1`` in plan order
    (``active`` then sees the new indices); the amplitude scale uses the kept
    count.  The returned iterator then has ``.channel_plan`` (kept, dropped,
    nch, nch_max, T, fs_max).  Default None: unchanged behaviour.
    """
    cplan = None
    if limit_fs is not None:
        cplan = plan_channels(band, limit_fs, f_eng, min_per_system)
        band = dataclasses.replace(band, svs=cplan.kept,
                                   sv_el_deg=[])
    fs = float(band.fs)
    bs = int(band.block_samples)
    nsv = len(band.svs)
    if nsv > W.MAX_CH:
        raise ValueError(f"{nsv} channels > MAX_CH={W.MAX_CH}")
    statics = [_Static(sv, ch) for ch, sv in enumerate(band.svs)]
    for st in statics:
        if st.L != cmdfmt.CODE_LEN[st.kind]:
            raise ValueError(f"channel {st.ch}: code_len {st.L} does not match "
                             f"kind {st.kind}")
        rom = roms.code_chips(st.kind, st.rom_prn)
        if not np.array_equal(np.ctypeslib.as_array(st.sv.code, (st.L,)), rom):
            raise ValueError(f"channel {st.ch}: primary code differs from the "
                             f"ROM code for kind {st.kind} prn {st.sv.prn}")
    scale = 0.25 * float(out_lsb) / math.sqrt(nsv) if nsv else float(out_lsb)
    sample_offset = int(sample_offset)
    nblk = (int(band.total_samples) - sample_offset) // bs
    if n_blocks is not None:
        nblk = min(nblk, int(n_blocks))
    gains = [float(st.sv.gain) for st in statics]
    cbocs = [int(st.sv.cboc) if st.sv.sub_carrier_hz > 0.0 else 0
             for st in statics]
    coefs = [_coefs(g, scale, c) for g, c in zip(gains, cbocs)]

    def gen():
        for j in range(nblk):
            recs = []
            for st in statics:
                if active is not None and not active(st.ch, j):
                    continue
                sv = st.sv
                f, ph, rate, cp0 = _kinematics(sv, st.L, st.rate_nom, j * bs + sample_offset, fs)
                cinc, cph = _carr_words(f, ph, fs)
                lim = st.L * _FRAC
                cp_fx = _rnd((cp0 % st.L) * _FRAC)
                if cp_fx >= lim:
                    cp_fx -= lim
                sp, bits = _sign_for(st, cp0, rate, fs, bs)
                code_inc = _rnd(rate / fs * _FRAC)
                recs.append(cmdfmt.ChannelBlock(
                    ch=st.ch, prn=st.rom_prn, kind=st.kind, code_len=st.L,
                    valid=True, carr_inc=cinc, carr_phase0=cph,
                    code_inc=code_inc, code_phase0=cp_fx, sign_phase0=sp,
                    sign_idx0=0, sign_bits=bits,
                    boc=int(sv.sub_carrier_hz > 0.0), cboc=cbocs[st.ch],
                    coef=coefs[st.ch]))
            yield first_block + j, recs

    if cplan is None:
        return gen()
    return _Stream(gen(), cplan)
