"""NavIC (IRNSS) L5-SPS navigation message encoder
(ISRO-IRNSS-ICD-SPS-1.1, Aug 2017).

Master Frame = 4 subframes x 600 symbols (50 sps, 12 s/subframe). Only
subframes 1 (clock + partial ephemeris) and 2 (remaining Kepler
ephemeris) carry data useful for a position fix; subframes 3/4
(message-based almanac/UTC/etc.) are out of scope and are not emitted --
the stream cycles 1, 2, 1, 2, ...

Each subframe: 16-bit uncoded 0xEB90 sync word, then 584 symbols of
FEC-encoded + block-interleaved data (292 raw bits: 30-bit header +
232-bit payload + 24-bit CRC-24Q + 6 zero tail bits). FEC is rate-1/2 K=7
(G1=0o171, G2=0o133 inverted) -- the same convention as
``cnav_encode``'s own ``_Conv`` -- but with a FRESH coder per subframe:
NavIC's is a block code (not tail-biting/continuous like CNAV's), and the
6 zero tail bits flush the register back to state 0 before the next
subframe starts.

Interleaving: 584 symbols written into a 73-column x 8-row block
column-by-column, then read out row-by-row (Table 9).
"""
from __future__ import annotations

import math

import numpy as np

from backend.analysis import _navic_layout as L
from backend.analysis._crc import crc24q

_G1 = 0o171
_G2 = 0o133
_SEMI = 1.0 / math.pi                        # radians -> semicircles


def _f(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if (math.isnan(v) or math.isinf(v)) else v


def bits_of(value: int, n: int) -> list[int]:
    value = int(value) & ((1 << n) - 1)
    return [(value >> (n - 1 - i)) & 1 for i in range(n)]


def _enc_field(kind: str, scale: float, nbits: int, phys: float) -> list[int]:
    q = int(round(_f(phys) / scale))
    if kind == "s":
        lo, hi = -(1 << (nbits - 1)), (1 << (nbits - 1)) - 1
        q = max(lo, min(hi, q))
        q &= (1 << nbits) - 1
    else:
        q = max(0, min((1 << nbits) - 1, q))
    return bits_of(q, nbits)


def _parity(x: int) -> int:
    return bin(x).count("1") & 1


class _Conv:
    """Rate-1/2 K=7 convolutional coder, G2 inverted. A fresh instance is
    used per subframe (see module docstring) -- state never carries across
    subframes, unlike cnav_encode._Conv's continuous stream."""

    def __init__(self) -> None:
        self.reg = 0

    def encode(self, data) -> list[int]:
        out: list[int] = []
        for b in data:
            self.reg = ((self.reg << 1) | (int(b) & 1)) & 0x7F
            out.append(_parity(self.reg & _G1))
            out.append(_parity(self.reg & _G2) ^ 1)
        return out


def _interleave(symbols: list[int]) -> list[int]:
    """584 symbols written column-by-column into a 73x8 block, read out
    row-by-row (Table 9: 73 columns x 8 rows)."""
    arr = np.asarray(symbols, dtype=np.int64).reshape(
        L.INTERLEAVE_COLS, L.INTERLEAVE_ROWS)
    return arr.T.reshape(-1).tolist()


def _payload(sf_id: int, values: dict) -> list[int]:
    bits: list[int] = []
    for name, kind, scale, nbits in L.LAYOUT[sf_id]:
        bits += _enc_field(kind, scale, nbits, values.get(name, 0.0))
    if len(bits) != L.DATA_BITS:
        raise ValueError(f"subframe {sf_id} payload {len(bits)} != "
                         f"{L.DATA_BITS} bits")
    return bits


def _resolve_1(eph: dict) -> dict:
    return {
        "wn": eph.get("gps_week", 0),
        "af0": eph.get("af0", 0.0),
        "af1": eph.get("af1", 0.0),
        "af2": eph.get("af2", 0.0),
        "ura": 0,
        "toc": eph.get("toc", eph.get("toe", 0.0)),
        "tgd": eph.get("tgd", 0.0),
        "delta_n": _f(eph.get("delta_n", 0.0)) * _SEMI,
        "iodec": 0,
        "reserved1": 0,
        "l5_flag": 0,
        "s_flag": 0,
        "cuc": eph.get("cuc", 0.0),
        "cus": eph.get("cus", 0.0),
        "cic": eph.get("cic", 0.0),
        "cis": eph.get("cis", 0.0),
        "crc": eph.get("crc", 0.0),
        "crs": eph.get("crs", 0.0),
        "idot": _f(eph.get("idot", 0.0)) * _SEMI,
        "spare1": 0,
    }


def _resolve_2(eph: dict) -> dict:
    return {
        "m0": _f(eph.get("m0", 0.0)) * _SEMI,
        "toe": eph.get("toe", 0.0),
        "e": eph.get("e", 0.0),
        "sqrt_a": _f(eph.get("sqrtA", 0.0)),
        "omega0": _f(eph.get("omega0", 0.0)) * _SEMI,
        "omega": _f(eph.get("omega", 0.0)) * _SEMI,
        "omega_dot": _f(eph.get("omega_dot", 0.0)) * _SEMI,
        "i0": _f(eph.get("i0", 0.0)) * _SEMI,
        "spare2": 0,
    }


_RESOLVE = {1: _resolve_1, 2: _resolve_2}


def build_subframe(sf_id: int, tow_17: int, eph: dict) -> list[int]:
    """600 output symbols (0/1) for one subframe: 16-bit sync word + 584
    FEC-encoded/interleaved symbols."""
    values = _RESOLVE[sf_id](eph or {})
    # Table 10: subframe ID 1 -> code "00", subframe ID 2 -> code "01".
    sfid_code = {1: 0b00, 2: 0b01}[sf_id]
    header = ([0] * 8                        # TLM -- reserved, all zero
              + bits_of(int(tow_17), 17)     # TOWC
              + [0]                           # Alert
              + [0]                           # Autonav
              + bits_of(sfid_code, 2)         # Subframe ID
              + [0])                          # Spare
    assert len(header) == L.HEADER_BITS, len(header)
    body = header + _payload(sf_id, values)
    assert len(body) == L.HEADER_BITS + L.DATA_BITS
    body = body + crc24q(body) + [0] * L.TAIL_BITS
    assert len(body) == L.SUBFRAME_BITS, len(body)
    coded = _Conv().encode(body)
    assert len(coded) == L.FEC_SYMBOLS, len(coded)
    interleaved = _interleave(coded)
    sync = bits_of(L.SYNC_WORD, L.SYNC_BITS)
    return sync + interleaved


def nav_stream(eph, header, week, sow, duration_s, *, prn, eph_by_prn=None):
    """(+/-1 int8 symbols at 50 sym/s, 50.0). Cycles subframes [1, 2], one
    600-symbol (12 s) subframe per TOWC-numbered 12 s slot. ``header`` is
    unused -- NavIC's ionosphere/UTC data lives in subframe 4 messages,
    out of scope for this closed-loop-fix path."""
    n_sf = max(1, math.ceil((math.ceil(float(duration_s)) + 24) / 12))
    # Subframes sit on the 12 s grid at or before ``sow`` (see cnav_encode).
    tow0 = int(float(sow) // 12.0)
    eph = dict(eph or {})
    eph.setdefault("gps_week", int(week))
    bits: list[int] = []
    for i in range(n_sf):
        # By 12 s slot of the week (master frame = slots 1..4; 3/4 are out
        # of scope and repeat 1/2), not by stream position: a stream started
        # later must continue the same sequence.
        sf_id = 1 if (tow0 + i) % 2 == 0 else 2
        tow = tow0 + i + 1               # next subframe start, 12 s units
        bits.extend(build_subframe(sf_id, tow, eph))
    arr = np.array([1 if s == 0 else -1 for s in bits], dtype=np.int8)
    return arr, 50.0
