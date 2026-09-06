"""IS-GPS-200 LNAV bit assembly for the native engine.

Pure functions: no I/O. Everything downstream (``engine.py`` wiring, the
decoder round-trip) builds on the primitives here. Parity follows
IS-GPS-200 Table 20-XIV: the six parity bits are computed over the 24
*source* data bits plus the previous word's last two bits (D29*, D30*),
and the 24 transmitted data bits are the source bits XORed with D30*.
"""
from __future__ import annotations

# IS-GPS-200 Table 20-XIV parity equations. 1-indexed source data bits.
_EQ = {
    25: (1, 2, 3, 5, 6, 10, 11, 12, 13, 14, 17, 18, 20, 23),
    26: (2, 3, 4, 6, 7, 11, 12, 13, 14, 15, 18, 19, 21, 24),
    27: (1, 3, 4, 5, 7, 8, 12, 13, 14, 15, 16, 19, 20, 22),
    28: (2, 4, 5, 6, 8, 9, 13, 14, 15, 16, 17, 20, 21, 23),
    29: (1, 3, 5, 6, 7, 9, 10, 14, 15, 16, 17, 18, 21, 22, 24),
    30: (3, 5, 6, 8, 9, 10, 11, 13, 15, 19, 22, 23, 24),
}
# Which previous-word bit each parity equation folds in.
_PREV = {25: "D29", 26: "D30", 27: "D29", 28: "D30", 29: "D30", 30: "D29"}

PREAMBLE = [1, 0, 0, 0, 1, 0, 1, 1]  # 0x8B


def bits_of(value: int, n: int) -> list[int]:
    """Big-endian bit list of ``value`` in ``n`` bits."""
    return [(value >> (n - 1 - i)) & 1 for i in range(n)]


def twos(value: float, scale: float, n: int) -> int:
    """Rounded two's-complement raw integer, masked to ``n`` bits."""
    raw = int(round(value / scale))
    if raw < 0:
        raw += 1 << n
    return raw & ((1 << n) - 1)


def parity_bits(source24, D29_prev: int, D30_prev: int) -> list[int]:
    """The six parity bits [D25..D30] for a word whose 24 source data bits
    are ``source24`` and whose previous word ended in (D29*, D30*)."""
    d = list(source24)
    if len(d) != 24:
        raise ValueError("source24 must be 24 bits")
    out = []
    for k in (25, 26, 27, 28, 29, 30):
        acc = D29_prev if _PREV[k] == "D29" else D30_prev
        for idx in _EQ[k]:
            acc ^= d[idx - 1]
        out.append(acc)
    return out


def make_word(source24, D29_prev: int, D30_prev: int) -> list[int]:
    """30-bit transmitted word: 24 data bits (XORed with D30*) + 6 parity."""
    d_src = list(source24)
    if len(d_src) != 24:
        raise ValueError("source24 must be 24 bits")
    p = parity_bits(d_src, D29_prev, D30_prev)
    d_tx = [b ^ D30_prev for b in d_src]
    return d_tx + p


def HOW_solve_bits(tow_count: int, subframe_id: int,
                   D29_prev: int, D30_prev: int) -> list[int]:
    """24 source bits for a HOW word with bits 23-24 chosen so the word's
    trailing computed parity (D29, D30) are both zero (IS-GPS-200 20.3.2)."""
    base = (bits_of(tow_count & 0x1FFFF, 17)
            + [0, 0] + bits_of(subframe_id & 0x7, 3))  # 22 bits
    for t23 in (0, 1):
        for t24 in (0, 1):
            src = base + [t23, t24]
            w = make_word(src, D29_prev, D30_prev)
            if w[28] == 0 and w[29] == 0:
                return src
    raise AssertionError("no HOW solve bits found")


# --- TLM / HOW / subframe skeleton --------------------------------------

def tlm_word(D29_prev: int, D30_prev: int) -> list[int]:
    """Word 1: preamble + 14-bit TLM message (0) + integrity + reserved."""
    src = PREAMBLE + bits_of(0, 14) + [0, 0]
    return make_word(src, D29_prev, D30_prev)


def how_word(tow_count: int, subframe_id: int,
             D29_prev: int, D30_prev: int) -> list[int]:
    """Word 2: 17-bit TOW-count + alert/A-S + 3-bit subframe id, trailing
    parity solved to zero."""
    src = HOW_solve_bits(tow_count, subframe_id, D29_prev, D30_prev)
    return make_word(src, D29_prev, D30_prev)


def subframe(words_src, tow_count: int, subframe_id: int,
            D29_start: int = 0, D30_start: int = 0) -> list[int]:
    """Assemble a 300-bit subframe: TLM, HOW, then eight 24-bit source
    words (3..10), tracking D29*/D30* across word boundaries.

    ``tow_count`` is the count for *this* subframe; the HOW carries the
    count of the next subframe start (IS-GPS-200 20.3.3.2).
    """
    if len(words_src) != 8 or any(len(w) != 24 for w in words_src):
        raise ValueError("subframe needs 8 source words of 24 bits each")
    out: list[int] = []
    d29, d30 = D29_start, D30_start
    w1 = tlm_word(d29, d30)
    out += w1
    d29, d30 = w1[28], w1[29]
    w2 = how_word((tow_count + 1) & 0x1FFFF, subframe_id, d29, d30)
    out += w2
    d29, d30 = w2[28], w2[29]
    for src in words_src:
        w = make_word(list(src), d29, d30)
        out += w
        d29, d30 = w[28], w[29]
    return out


# --- Subframe 1: clock, health, URA, IODC, Tgd, Toc -------------------

def subframe1(eph: dict, week: int, tow_count: int) -> list[int]:
    """300 bits. Uses eph keys af0/af1/af2 (s, s/s, s/s^2), toc (s),
    tgd (s), iodc, health. URA index fixed at 0, CA/P-on-L2 = 01,
    L2-P-data flag = 0."""
    iodc = int(eph.get("iodc", 0)) & 0x3FF
    health = int(eph.get("health", 0)) & 0x3F
    w3 = (bits_of(week & 0x3FF, 10) + [0, 1] + bits_of(0, 4)
          + bits_of(health, 6) + bits_of(iodc >> 8, 2))
    w4 = [0] + bits_of(0, 23)
    w5 = bits_of(0, 24)
    w6 = bits_of(0, 24)
    w7 = bits_of(0, 16) + bits_of(twos(eph["tgd"], 2 ** -31, 8), 8)
    w8 = bits_of(iodc & 0xFF, 8) + bits_of(round(eph["toc"] / 16) & 0xFFFF, 16)
    w9 = (bits_of(twos(eph["af2"], 2 ** -55, 8), 8)
          + bits_of(twos(eph["af1"], 2 ** -43, 16), 16))
    w10 = bits_of(twos(eph["af0"], 2 ** -31, 22), 22) + [0, 0]
    return subframe([w3, w4, w5, w6, w7, w8, w9, w10], tow_count, 1)
