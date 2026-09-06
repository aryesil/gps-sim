"""SP-D: BeiDou B1I D1 / D2 navigation message encoder.

The *coding layer* is BDS-SIS-ICD-B1I exact:

* 50 bps (D1, MEO/IGSO) or 500 bps (D2, GEO) NRZ.
* Subframe = 10 words x 30 bits = 300 bits, 6 s (D1) / 0.6 s (D2).
* Word 1: first 15 bits are the preamble ``11100010010`` + 1 reserved + 3-bit
  FraID, sent **without** BCH; the last 15 bits are BCH(15,11,1) over 11
  info bits (SOW high bits).
* Words 2..10: two BCH(15,11,1) codewords per word, bit-interleaved
  ``a0 b0 a1 b1 ... a14 b14``.
* BCH(15,11,1): systematic, generator ``g(x) = x^4 + x + 1``.

The ephemeris / clock fields are laid into the per-subframe information-bit
stream MSB-first with the ICD per-field widths and scale factors, contiguous
(the ICD's split of a few fields across word boundaries is not reproduced --
irrelevant to a simulator whose own :func:`decode_subframe` inverts it).
Subframes 1-3 carry the real broadcast record; 4-5 are valid-BCH zero pages.

Verified by the encoder unit tests (preamble, BCH reference, interleave
permutation) and a clean-channel field round-trip via :func:`decode_frame`.
"""
from __future__ import annotations

import math

import numpy as np

PREAMBLE = [1, 1, 1, 0, 0, 0, 1, 0, 0, 1, 0]      # 0x712 >> ... 11 bits
SYM_RATE_D1 = 50.0
SYM_RATE_D2 = 500.0
_SC = math.pi
_BCH_G = 0b10011                                   # x^4 + x + 1


def _f(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(v) or math.isinf(v) else v


def bits_of(value: int, n: int) -> list[int]:
    v = int(value) & ((1 << n) - 1)
    return [(v >> (n - 1 - i)) & 1 for i in range(n)]


def twos(value, scale, n) -> list[int]:
    q = int(round(_f(value) / scale))
    lo, hi = -(1 << (n - 1)), (1 << (n - 1)) - 1
    return bits_of(max(lo, min(hi, q)), n)


def uns(value, scale, n) -> list[int]:
    q = int(round(_f(value) / scale))
    return bits_of(max(0, min((1 << n) - 1, q)), n)


# --------------------------------------------------------------------------
# BCH(15, 11, 1)
# --------------------------------------------------------------------------
def bch_encode(info11: list[int]) -> list[int]:
    """Systematic BCH(15,11,1): 11 info bits -> 15 bits (info + 4 parity)."""
    assert len(info11) == 11
    reg = 0
    for b in info11:
        top = (reg >> 3) & 1
        reg = ((reg << 1) & 0xF)
        if top ^ (b & 1):
            reg ^= (_BCH_G & 0xF)
    return list(info11) + bits_of(reg, 4)


def bch_decode(word15: list[int]) -> list[int]:
    """Return the 11 info bits (systematic prefix). Clean channel: parity is
    not used to correct, only the prefix is taken."""
    return list(word15[:11])


def interleave2(a15: list[int], b15: list[int]) -> list[int]:
    out = []
    for i in range(15):
        out.append(a15[i])
        out.append(b15[i])
    return out


def deinterleave2(word30: list[int]) -> tuple[list[int], list[int]]:
    return word30[0::2], word30[1::2]


# --------------------------------------------------------------------------
# subframe assembly
# --------------------------------------------------------------------------
def _info_stream(sf_id: int, eph: dict, sow: int, week: int) -> list[int]:
    """The information bits for words 2..10 of a subframe: 9 words x 22 info
    bits = 198 bits. Field layout is contiguous MSB-first (see module doc)."""
    b: list[int] = []
    if sf_id == 1:
        b += bits_of(int(_f(eph.get("health", 0))) & 1, 1)          # SatH1
        b += bits_of(int(_f(eph.get("iodc", 0))) & 0x1F, 5)         # AODC
        b += bits_of(0, 4)                                          # URAI
        b += bits_of(int(week) & 0x1FFF, 13)                        # WN
        b += uns(eph.get("toc"), 8.0, 17)                          # toc
        b += twos(eph.get("tgd", 0.0), 1e-10, 10)                  # TGD1
        b += twos(0.0, 1e-10, 10)                                  # TGD2
        b += twos(eph.get("af0"), 2 ** -33, 24)                    # a0
        b += twos(eph.get("af1"), 2 ** -50, 22)                    # a1
        b += twos(eph.get("af2"), 2 ** -66, 11)                    # a2
        b += bits_of(int(_f(eph.get("iode", 0))) & 0x1F, 5)        # AODE
    elif sf_id == 2:
        b += twos(_v(eph, "delta_n") / _SC, 2 ** -43, 16)
        b += twos(eph.get("cuc"), 2 ** -31, 18)
        b += twos(_v(eph, "m0") / _SC, 2 ** -31, 32)
        b += uns(eph.get("e"), 2 ** -33, 32)
        b += twos(eph.get("cus"), 2 ** -31, 18)
        b += twos(eph.get("crc"), 2 ** -6, 18)
        b += twos(eph.get("crs"), 2 ** -6, 18)
        b += uns(eph.get("sqrtA"), 2 ** -19, 32)
        b += uns(eph.get("toe"), 8.0, 17)
    elif sf_id == 3:
        b += twos(_v(eph, "i0") / _SC, 2 ** -31, 32)
        b += twos(eph.get("cic"), 2 ** -31, 18)
        b += twos(_v(eph, "omega_dot") / _SC, 2 ** -43, 24)
        b += twos(eph.get("cis"), 2 ** -31, 18)
        b += twos(_v(eph, "idot") / _SC, 2 ** -43, 14)
        b += twos(_v(eph, "omega0") / _SC, 2 ** -31, 32)
        b += twos(_v(eph, "omega") / _SC, 2 ** -31, 32)
    # pad / truncate to exactly 189 bits (9 bits of the 198-bit words-2..10
    # info stream are reserved for the low SOW bits, prepended in build_subframe)
    b = (b + [0] * 189)[:189]
    return b


def _v(eph, k):
    return _f(eph.get(k))


def build_subframe(sf_id: int, eph: dict, sow: int, week: int) -> list[int]:
    """One 300-bit D1/D2 subframe."""
    # word 1: preamble(11) + reserved(1) + FraID(3) raw, then BCH over SOW[19:9]
    raw15 = PREAMBLE + [0] + bits_of(sf_id, 3)
    sow20 = bits_of(int(sow) & 0xFFFFF, 20)
    w1_bch = bch_encode(sow20[:11])
    words = [raw15 + w1_bch]

    # words 2..10 info stream: 9 low SOW bits then the 189-bit payload = 198.
    info = sow20[11:] + _info_stream(sf_id, eph, sow, week)
    assert len(info) == 198
    for w in range(9):
        a = bch_encode(info[w * 22:w * 22 + 11])
        b = bch_encode(info[w * 22 + 11:w * 22 + 22])
        words.append(interleave2(a, b))
    flat = [bit for wd in words for bit in wd]
    assert len(flat) == 300
    return flat


_D1_CYCLE = [1, 2, 3, 4, 5]


def nav_stream(eph: dict, week: int, tow0_sow: float, duration_s: float,
               *, d2: bool = False, eph_by_prn=None) -> tuple[np.ndarray, float]:
    """Deterministic D1 (or D2) symbol stream, ``int8`` {-1,+1}. D1 is 50
    sym/s with a 6 s subframe; D2 is 500 sym/s with a 0.6 s subframe.
    """
    rate = SYM_RATE_D2 if d2 else SYM_RATE_D1
    sf_len_s = 0.6 if d2 else 6.0
    nsf = int(math.ceil((math.ceil(duration_s) + 30) / sf_len_s))
    sow0 = int(round(tow0_sow))
    syms: list[int] = []
    for i in range(nsf):
        sf_id = _D1_CYCLE[i % 5]
        sow = (sow0 + int(round(i * sf_len_s))) % 604800
        syms.extend(build_subframe(sf_id, eph, sow, week))
    a = np.asarray(syms, dtype=np.int8)
    return np.where(a > 0, np.int8(1), np.int8(-1)).astype(np.int8), rate


# --------------------------------------------------------------------------
# clean-channel decode (for the round-trip tests only)
# --------------------------------------------------------------------------
def decode_subframe(sf300: list[int]) -> dict:
    words = [sf300[i * 30:(i + 1) * 30] for i in range(10)]
    assert words[0][:11] == PREAMBLE, "preamble mismatch"
    fra_id = int("".join(map(str, words[0][12:15])), 2)
    info: list[int] = []
    for w in words[1:]:
        a, b = deinterleave2(w)
        info += bch_decode(a) + bch_decode(b)
    info = info[9:]                                 # drop the SOW-low carry
    return {"fra_id": fra_id, "info": info}


def decode_frame(stream: np.ndarray) -> dict[int, list[int]]:
    """Map subframe id -> its 198-bit info stream, scanning for the preamble."""
    bits = (np.asarray(stream) > 0).astype(int).tolist()
    out: dict[int, list[int]] = {}
    i = 0
    while i + 300 <= len(bits):
        if bits[i:i + 11] == PREAMBLE:
            d = decode_subframe(bits[i:i + 300])
            out.setdefault(d["fra_id"], d["info"])
            i += 300
        else:
            i += 1
    return out
