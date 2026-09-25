"""Galileo E5a-I F/NAV navigation message encoder (Galileo OS SIS ICD
Section 4.2). Builds a structurally spec-accurate F/NAV page stream: 500
symbols @ 50 sym/s (10 s nominal page) = a 12-symbol uncoded preamble
(``101101110000``) followed by 488 FEC symbols -- rate 1/2, constraint
length 7 convolutional coding (G1 = 0o171, G2 = 0o133 with G2 inverted)
over a 244-bit pre-FEC block (214-bit message + 24-bit CRC-24Q + 6 tail
zeros), then an 8x61 block interleaver.

Word types 1-4 (clock/BGD/GST, ephemeris parts 1-3) are implemented, one
per page in a repeating 4-page cycle -- a receiver recovers a full
ephemeris set within 40 s. Iono/almanac/UTC/GST-GPS-conversion fields
(word types 1, 4, 5, 6) are transmitted as zero; this project's phases
only ever consume word types 1-4's clock/ephemeris/BGD fields for a
position fix. As with ``inav_encode.py``, each page independently CRCs
and FEC-flushes rather than the continuous bit stream real hardware
encodes -- a self-consistent round trip, not byte-exact to a real
receiver.

Field bit-allocation/scale-factors are driven by the shared table in
``backend.analysis._fnav_layout`` so the decoder stays in lockstep.

Bit -> chip convention: ``bit=0 -> chip+1, bit=1 -> chip-1`` -- the
*opposite* of ``inav_encode.py``'s convention, and deliberately so: F/NAV
symbols are demodulated by ``fnav_decode.demod_symbols_e5a``, a thin
wrapper around ``cnav_decode._demod`` (E5a-I's 10230-chip/CS20-secondary
structure mirrors L5's I5/NH10 exactly), whose hard-decision rule
(``bit=1 when real(corr)<0``) is the CNAV convention, not I/NAV's.
"""
from __future__ import annotations

import math

import numpy as np

from backend.analysis import _fnav_layout as L
from backend.analysis._crc import crc24q

_PREAMBLE = [1, 0, 1, 1, 0, 1, 1, 1, 0, 0, 0, 0]     # 12 uncoded symbols
_G1 = 0o171
_G2 = 0o133
_SC = math.pi                                          # semicircle <-> radian


# --------------------------------------------------------------------------
# bit helpers
# --------------------------------------------------------------------------
def bits_of(value: int, n: int) -> list[int]:
    v = int(value) & ((1 << n) - 1)
    return [(v >> (n - 1 - i)) & 1 for i in range(n)]


def _f(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(v) or math.isinf(v) else v


def twos(value: float, scale: float, n: int) -> int:
    q = int(round(_f(value) / scale))
    lo, hi = -(1 << (n - 1)), (1 << (n - 1)) - 1
    return max(lo, min(hi, q))


def uns(value: float, scale: float, n: int) -> int:
    q = int(round(_f(value) / scale))
    return max(0, min((1 << n) - 1, q))


def _rad2sc(x) -> float:
    """Radians -> semicircles, NaN/None-safe."""
    return _f(x) / _SC


# --------------------------------------------------------------------------
# FEC: rate 1/2, K=7 convolutional, G2 inverted (per-page flush, from state 0)
# --------------------------------------------------------------------------
def _parity(x: int) -> int:
    return bin(x).count("1") & 1


def conv_encode(data: list[int]) -> list[int]:
    reg = 0
    out: list[int] = []
    for b in data:
        reg = ((reg << 1) | (int(b) & 1)) & 0x7F
        out.append(_parity(reg & _G1))
        out.append(_parity(reg & _G2) ^ 1)
    return out


def interleave_8x61(sym: list[int]) -> list[int]:
    """Block interleaver: write 8 rows x 61 columns by rows, read by columns."""
    assert len(sym) == L.CODED_SYMS
    a = np.asarray(sym).reshape(8, 61)
    return a.T.reshape(L.CODED_SYMS).tolist()


def deinterleave_8x61(sym: list[int]) -> list[int]:
    a = np.asarray(sym).reshape(61, 8)
    return a.T.reshape(L.CODED_SYMS).tolist()


# --------------------------------------------------------------------------
# word-type payloads (214 bits each incl. the 6-bit type)
# --------------------------------------------------------------------------
def _iodnav(eph: dict) -> int:
    return int(_f(eph.get("iode", 0))) & 0x3FF


def _values_word1(eph: dict, prn: int, week: int, tow: int) -> dict:
    return {
        "type": 1, "prn": prn, "iodnav": _iodnav(eph),
        "toc": eph.get("toc", eph.get("toe", 0.0)),
        "af0": eph.get("af0", 0.0), "af1": eph.get("af1", 0.0),
        "af2": eph.get("af2", 0.0), "tgd": eph.get("tgd_e5a", eph.get("tgd", 0.0)),
        "week": int(week), "tow": int(tow),
    }


def _values_word2(eph: dict, week: int, tow: int) -> dict:
    return {
        "type": 2, "iodnav": _iodnav(eph),
        "m0": _rad2sc(eph.get("m0")), "omega_dot": _rad2sc(eph.get("omega_dot")),
        "e": eph.get("e", 0.0), "sqrtA": eph.get("sqrtA", 0.0),
        "omega0": _rad2sc(eph.get("omega0")), "idot": _rad2sc(eph.get("idot")),
        "week": int(week), "tow": int(tow),
    }


def _values_word3(eph: dict, week: int, tow: int) -> dict:
    return {
        "type": 3, "iodnav": _iodnav(eph),
        "i0": _rad2sc(eph.get("i0")), "omega": _rad2sc(eph.get("omega")),
        "delta_n": _rad2sc(eph.get("delta_n")),
        "cuc": eph.get("cuc", 0.0), "cus": eph.get("cus", 0.0),
        "crc": eph.get("crc", 0.0), "crs": eph.get("crs", 0.0),
        "toe": eph.get("toe", 0.0),
        "week": int(week), "tow": int(tow),
    }


def _values_word4(eph: dict, tow: int) -> dict:
    return {
        "type": 4, "iodnav": _iodnav(eph),
        "cic": eph.get("cic", 0.0), "cis": eph.get("cis", 0.0),
        "tow": int(tow),
    }


_VALUES = {1: _values_word1, 2: _values_word2, 3: _values_word3, 4: _values_word4}


def _encode_word(wtype: int, values: dict) -> list[int]:
    out: list[int] = []
    for name, kind, scale, n in L.LAYOUT[wtype]:
        if name == "_reserved":
            out.extend([0] * n)
            continue
        v = values.get(name, 0)
        q = twos(v, scale, n) if kind == "s" else uns(v, scale, n)
        out.extend(bits_of(q, n))
    assert len(out) == L.MSG_BITS, (wtype, len(out))
    return out


def word_bits(wtype: int, eph: dict, prn: int, tow: int, week: int) -> list[int]:
    if wtype == 1:
        values = _values_word1(eph, prn, week, tow)
    elif wtype == 2:
        values = _values_word2(eph, week, tow)
    elif wtype == 3:
        values = _values_word3(eph, week, tow)
    else:
        values = _values_word4(eph, tow)
    return _encode_word(wtype, values)


# --------------------------------------------------------------------------
# page assembly
# --------------------------------------------------------------------------
def build_page(wtype: int, eph: dict, prn: int, tow: int, week: int) -> list[int]:
    """One 500-symbol nominal page (preamble + FEC-coded, interleaved body)."""
    message = word_bits(wtype, eph, prn, tow, week)       # 214 bits incl. type
    crc = crc24q(message)                                  # 24 bits
    pre_fec = message + crc + [0] * L.TAIL_BITS            # 244 bits
    coded = conv_encode(pre_fec)                            # 488 symbols
    body = interleave_8x61(coded)
    page = _PREAMBLE + body
    assert len(page) == L.PAGE_SYMS, len(page)
    return page


def _viterbi_free_decode(coded: list[int]) -> list[int]:
    """Exact inverse of :func:`conv_encode` for a clean (noise-free) stream."""
    reg = 0
    out = []
    for i in range(0, len(coded), 2):
        c1 = coded[i]
        for cand in (0, 1):
            r = ((reg << 1) | cand) & 0x7F
            if _parity(r & _G1) == c1 and (_parity(r & _G2) ^ 1) == coded[i + 1]:
                out.append(cand)
                reg = r
                break
        else:                                    # pragma: no cover - clean only
            out.append(0)
            reg = (reg << 1) & 0x7F
    return out


def decode_word(page_syms: list[int]) -> tuple[int, list[int]]:
    """Recover ``(word_type, message[214])`` from a clean page produced by
    :func:`build_page` -- for the encoder self-test, not a noise-tolerant
    receiver (see ``fnav_decode.decode_messages`` for that)."""
    coded = deinterleave_8x61(list(page_syms[L.PREAMBLE_SYMS:]))
    pre_fec = _viterbi_free_decode(coded)
    message = pre_fec[:L.MSG_BITS]
    wtype = int("".join(str(b) for b in message[:6]), 2)
    return wtype, message


def check_page_crc(page_syms: list[int]) -> bool:
    """Re-verify the CRC of a page produced by :func:`build_page`."""
    coded = deinterleave_8x61(list(page_syms[L.PREAMBLE_SYMS:]))
    pre_fec = _viterbi_free_decode(coded)
    message = pre_fec[:L.MSG_BITS]
    crc = pre_fec[L.MSG_BITS:L.MSG_BITS + L.CRC_BITS]
    return crc24q(message) == crc


# --------------------------------------------------------------------------
# public entry
# --------------------------------------------------------------------------
_CYCLE = L.CYCLE       # (1, 2, 3, 4)


def nav_stream(eph: dict, prn: int, week: int, tow0_sow: float,
                duration_s: float, eph_by_prn=None) -> tuple[np.ndarray, float]:
    """Deterministic F/NAV symbol stream, ``int8`` in ``{-1, +1}`` at
    50 sym/s. Length covers ``duration_s`` rounded up to a whole 10 s page
    plus a 30 s guard, aligned so a page boundary sits on the 10 s GST grid.
    """
    npages = int(math.ceil((math.ceil(duration_s) + 30) / 10.0))
    tow0 = int(round(tow0_sow))
    tow0 -= tow0 % 10
    syms: list[int] = []
    for p in range(npages):
        tow = (tow0 + 10 * p) % 604800
        # Page type follows GST (page count since the week start), so a
        # stream started later continues the same schedule.
        wtype = _CYCLE[(tow // 10) % len(_CYCLE)]
        syms.extend(build_page(wtype, eph, prn, tow, week))
    bits = np.asarray(syms, dtype=np.int8)
    arr = np.where(bits > 0, np.int8(-1), np.int8(1))    # bit=0->+1, bit=1->-1
    return arr, 50.0
