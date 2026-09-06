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
