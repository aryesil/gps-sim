"""SP-D: Galileo E1-B I/NAV navigation message encoder.

Builds a structurally spec-accurate Galileo Open Service I/NAV symbol stream
(Galileo OS SIS ICD): 2 s nominal pages, each an *even* then an *odd* 250-
symbol page part at 250 sym/s. A page part is a 10-symbol synchronisation
pattern followed by 240 FEC symbols -- rate 1/2, constraint length 7
convolutional coding (G1 = 0o171, G2 = 0o133 with G2 inverted) over a
120-bit block (114 data + 6 tail zeros), then a 30x8 block interleaver.

Implemented word types: 1-5 (ephemeris + clock + iono/BGD/GST) plus type 0
(spare/time), transmitted one per page in a repeating 6-page cycle so a
receiver recovers the full ephemeris within 12 s. Reserved / SAR / SISA
fields are transmitted as zero. A 24-bit CRC-24Q covers the two page
parts' type+data fields; :func:`check_page_crc` re-verifies it.

No Galileo receiver ships in this repo; correctness is covered by the
encoder unit tests (sync offsets, FEC reference vector, interleaver
permutation, CRC self-check) and by acquisition surviving the modulation.
"""
from __future__ import annotations

import math

import numpy as np

SYNC_PATTERN = [0, 1, 0, 1, 1, 0, 0, 0, 0, 0]   # 10 symbols, even and odd
SYM_RATE_HZ = 250.0
_PAGE_SYMS = 500                                  # 2 s nominal page
_HALF_SYMS = 250
_SC = math.pi                                     # semicircle <-> radian

_G1 = 0o171
_G2 = 0o133
_CRC24Q = 0x1864CFB


# --------------------------------------------------------------------------
# bit helpers
# --------------------------------------------------------------------------
def bits_of(value: int, n: int) -> list[int]:
    """`value` as `n` big-endian bits (two's complement if negative)."""
    v = int(value) & ((1 << n) - 1)
    return [(v >> (n - 1 - i)) & 1 for i in range(n)]


def _f(x) -> float:
    """NaN/None-safe float: missing broadcast fields become 0."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(v) or math.isinf(v) else v


def twos(value: float, scale: float, n: int) -> int:
    """Quantise `value / scale` to an `n`-bit signed integer (saturating)."""
    q = int(round(_f(value) / scale))
    lo, hi = -(1 << (n - 1)), (1 << (n - 1)) - 1
    return max(lo, min(hi, q))


def uns(value: float, scale: float, n: int) -> int:
    q = int(round(_f(value) / scale))
    return max(0, min((1 << n) - 1, q))


def _crc24q(bits: list[int]) -> list[int]:
    """CRC-24Q (poly 0x1864CFB, init 0), MSB-first over `bits`."""
    reg = 0
    for b in bits:
        reg ^= (int(b) & 1) << 23
        reg <<= 1
        if reg & (1 << 24):
            reg ^= _CRC24Q
        reg &= 0xFFFFFF
    for _ in range(24):
        reg <<= 1
        if reg & (1 << 24):
            reg ^= _CRC24Q
        reg &= 0xFFFFFF
    return bits_of(reg, 24)


# --------------------------------------------------------------------------
# FEC: rate 1/2, K=7 convolutional, G2 inverted
# --------------------------------------------------------------------------
def _parity(x: int) -> int:
    return bin(x).count("1") & 1


def conv_encode(data: list[int]) -> list[int]:
    """Rate-1/2 K=7 convolutional code, G1=0o171 then (G2=0o133 inverted).
    Encoder starts in the all-zero state; `data` must carry its own tail.
    """
    reg = 0
    out: list[int] = []
    for b in data:
        reg = ((reg << 1) | (int(b) & 1)) & 0x7F
        out.append(_parity(reg & _G1))
        out.append(_parity(reg & _G2) ^ 1)
    return out


def interleave_30x8(sym: list[int]) -> list[int]:
    """Block interleaver: write 8 rows x 30 columns by rows, read by columns."""
    assert len(sym) == 240
    a = np.asarray(sym).reshape(8, 30)
    return a.T.reshape(240).tolist()


def deinterleave_30x8(sym: list[int]) -> list[int]:
    a = np.asarray(sym).reshape(30, 8)
    return a.T.reshape(240).tolist()


# --------------------------------------------------------------------------
# word-type payloads (128 bits each incl. the 6-bit type)
# --------------------------------------------------------------------------
def _iodnav(eph: dict) -> int:
    return int(_f(eph.get("iode", 0))) & 0x3FF


def word_type_1(eph, tow):
    b = bits_of(1, 6) + bits_of(_iodnav(eph), 10)
    b += bits_of(int(round(_f(eph.get("toe")) / 60.0)) & 0x3FFF, 14)
    b += bits_of(twos(eph.get("m0") / _SC, 2 ** -31, 32), 32)
    b += bits_of(uns(eph.get("e"), 2 ** -33, 32), 32)
    b += bits_of(uns(eph.get("sqrtA"), 2 ** -19, 32), 32)
    b += [0, 0]
    return b


def word_type_2(eph, tow):
    b = bits_of(2, 6) + bits_of(_iodnav(eph), 10)
    b += bits_of(twos(eph.get("omega0") / _SC, 2 ** -31, 32), 32)
    b += bits_of(twos(eph.get("i0") / _SC, 2 ** -31, 32), 32)
    b += bits_of(twos(eph.get("omega") / _SC, 2 ** -31, 32), 32)
    b += bits_of(twos(eph.get("idot") / _SC, 2 ** -43, 14), 14)
    b += [0, 0]
    return b


def word_type_3(eph, tow):
    b = bits_of(3, 6) + bits_of(_iodnav(eph), 10)
    b += bits_of(twos(eph.get("omega_dot") / _SC, 2 ** -43, 24), 24)
    b += bits_of(twos(eph.get("delta_n") / _SC, 2 ** -43, 16), 16)
    b += bits_of(twos(eph.get("cuc"), 2 ** -29, 16), 16)
    b += bits_of(twos(eph.get("cus"), 2 ** -29, 16), 16)
    b += bits_of(twos(eph.get("crc"), 2 ** -5, 16), 16)
    b += bits_of(twos(eph.get("crs"), 2 ** -5, 16), 16)
    b += bits_of(0, 8)                       # SISA
    return b


def word_type_4(eph, tow):
    b = bits_of(4, 6) + bits_of(_iodnav(eph), 10)
    b += bits_of(int(eph.get("prn", 0)) & 0x3F, 6)
    b += bits_of(twos(eph.get("cic"), 2 ** -29, 16), 16)
    b += bits_of(twos(eph.get("cis"), 2 ** -29, 16), 16)
    b += bits_of(int(round(_f(eph.get("toc")) / 60.0)) & 0x3FFF, 14)
    b += bits_of(twos(eph.get("af0"), 2 ** -34, 31), 31)
    b += bits_of(twos(eph.get("af1"), 2 ** -46, 21), 21)
    b += bits_of(twos(eph.get("af2"), 2 ** -59, 6), 6)
    b += [0, 0]
    return b


def word_type_5(eph, tow, week):
    b = bits_of(5, 6)
    b += bits_of(0, 11) + bits_of(0, 11) + bits_of(0, 14)      # iono a_i0..2
    b += bits_of(0, 5)                                          # region flags
    b += bits_of(twos(eph.get("tgd", 0.0), 2 ** -32, 10), 10)  # BGD E1-E5a
    b += bits_of(twos(eph.get("tgd", 0.0), 2 ** -32, 10), 10)  # BGD E1-E5b
    b += bits_of(0, 2) + bits_of(0, 2) + [0] + [0]             # HS/DVS
    b += bits_of(int(week) & 0xFFF, 12)
    b += bits_of(int(tow) & 0xFFFFF, 20)
    b += bits_of(0, 23)
    return b


def word_type_0(eph, tow, week):
    b = bits_of(0, 6) + bits_of(0, 2) + bits_of(0, 88)
    b += bits_of(int(week) & 0xFFF, 12)
    b += bits_of(int(tow) & 0xFFFFF, 20)
    return b


_CYCLE = [1, 2, 3, 4, 5, 0]


def word_bits(wtype: int, eph: dict, tow: int, week: int) -> list[int]:
    if wtype == 1:
        w = word_type_1(eph, tow)
    elif wtype == 2:
        w = word_type_2(eph, tow)
    elif wtype == 3:
        w = word_type_3(eph, tow)
    elif wtype == 4:
        w = word_type_4(eph, tow)
    elif wtype == 5:
        w = word_type_5(eph, tow, week)
    else:
        w = word_type_0(eph, tow, week)
    assert len(w) == 128, (wtype, len(w))
    return w


# --------------------------------------------------------------------------
# page assembly
# --------------------------------------------------------------------------
def build_page(wtype: int, eph: dict, tow: int, week: int) -> list[int]:
    """One 500-symbol nominal page (even part then odd part)."""
    word = word_bits(wtype, eph, tow, week)          # 128 bits incl. type
    data_k = word[:112]                              # even part data field
    data_j = word[112:128]                           # odd part data field (16)

    # I/NAV page part = 114 content bits + 6 tail zeros -> 120 -> FEC -> 240.
    #   even: even/odd(1) + page type(1) + data k 1..112 (112)          = 114
    #   odd : even/odd(1) + page type(1) + data k 113..128 (16)
    #         + reserved-1(40) + SAR(22) + spare(2) + CRC(24) + reserved-2(8)
    #                                                                   = 114
    # CRC-24Q covers the 196 bits preceding it, in transmission order.
    crc_in = ([0, 0] + data_k
              + [1, 0] + data_j + [0] * 40 + [0] * 22 + [0, 0])
    assert len(crc_in) == 196
    crc = _crc24q(crc_in)

    even_data = [0, 0] + data_k + [0] * 6                       # 120 bits
    odd_data = ([1, 0] + data_j + [0] * 40 + [0] * 22 + [0, 0]
                + crc + [0] * 8 + [0] * 6)                      # 120 bits
    assert len(even_data) == 120 and len(odd_data) == 120

    even = SYNC_PATTERN + interleave_30x8(conv_encode(even_data))
    odd = SYNC_PATTERN + interleave_30x8(conv_encode(odd_data))
    assert len(even) == _HALF_SYMS and len(odd) == _HALF_SYMS
    return even + odd


def check_page_crc(page_syms: list[int]) -> bool:
    """Re-verify the CRC of a page produced by :func:`build_page`."""
    even = page_syms[:_HALF_SYMS]
    odd = page_syms[_HALF_SYMS:]
    even_data = _viterbi_free_decode(deinterleave_30x8(even[10:]))
    odd_data = _viterbi_free_decode(deinterleave_30x8(odd[10:]))
    data_k = even_data[2:114]
    data_j = odd_data[2:18]
    crc_in = ([0, 0] + data_k + [1, 0] + data_j + [0] * 40 + [0] * 22 + [0, 0])
    off = 2 + 16 + 40 + 22 + 2
    return _crc24q(crc_in) == odd_data[off:off + 24]


def decode_word(page_syms: list[int]) -> tuple[int, list[int]]:
    """Recover ``(word_type, word_bits[128])`` from a clean page produced by
    :func:`build_page` -- deinterleave, undo the FEC, then splice the 112-bit
    even data field with the 16-bit odd data field. For the encoder tests and
    the QZSS-style round-trip checks; not a noise-tolerant receiver.
    """
    even = page_syms[:_HALF_SYMS]
    odd = page_syms[_HALF_SYMS:]
    even_data = _viterbi_free_decode(deinterleave_30x8(list(even[10:])))
    odd_data = _viterbi_free_decode(deinterleave_30x8(list(odd[10:])))
    word = even_data[2:114] + odd_data[2:18]
    wtype = int("".join(str(b) for b in word[:6]), 2)
    return wtype, word


def _viterbi_free_decode(coded: list[int]) -> list[int]:
    """Exact inverse of :func:`conv_encode` for a clean (noise-free) stream:
    walk the trellis deterministically using the G1 output, which for this
    code equals the newest register bit XOR older taps -- recover input bit
    by bit from the known encoder state."""
    reg = 0
    out = []
    for i in range(0, len(coded), 2):
        c1 = coded[i]
        # try input 0 and 1, pick the one whose (c1, c2) matches
        for cand in (0, 1):
            r = ((reg << 1) | cand) & 0x7F
            if _parity(r & _G1) == c1 and (_parity(r & _G2) ^ 1) == coded[i + 1]:
                out.append(cand)
                reg = r
                break
        else:                                   # pragma: no cover - clean only
            out.append(0)
            reg = (reg << 1) & 0x7F
    return out


# --------------------------------------------------------------------------
# public entry
# --------------------------------------------------------------------------
def nav_stream(eph: dict, week: int, tow0_sow: float, duration_s: float,
               eph_by_prn=None) -> np.ndarray:
    """Deterministic I/NAV symbol stream, ``int8`` in ``{-1, +1}`` at 250
    sym/s. Length covers ``duration_s`` rounded up to a whole 2 s page plus a
    30 s guard, aligned so a page boundary sits on the 2 s GST grid.
    """
    npages = int(math.ceil((math.ceil(duration_s) + 30) / 2.0))
    tow0 = int(round(tow0_sow))
    tow0 -= tow0 % 2
    syms: list[int] = []
    for p in range(npages):
        tow = (tow0 + 2 * p) % 604800
        # Word type follows GST, not the stream start: a stream started
        # later (live mode restarts it every segment) must continue the
        # same sequence instead of re-sending the first word type.
        wtype = _CYCLE[(tow // 2) % len(_CYCLE)]
        syms.extend(build_page(wtype, eph, tow, week))
    a = np.asarray(syms, dtype=np.int8)
    return np.where(a > 0, np.int8(1), np.int8(-1)).astype(np.int8)
