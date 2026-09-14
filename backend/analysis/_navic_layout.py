"""Shared NavIC (IRNSS) subframe 1/2 field layouts (ISRO-IRNSS-ICD-SPS-1.1,
Aug 2017, Table 11 / Table 12, verified against the primary-source PDF).

One ordered ``(name, kind, scale, nbits)`` list per subframe, walked
identically by ``navic_encode`` (physical value -> bits) and
``navic_decode`` (bits -> physical value) so the two never drift.

``kind``: ``"u"`` unsigned, ``"s"`` two's-complement signed. Angular
fields (m0, omega, omega0, i0, idot, delta_n) are carried in
**semicircles** (multiply by pi for radians), matching the ICD's own
units column directly.

Subframe 1 data section is 232 bits (bit 31..262 of the 292-bit
subframe); subframe 2's is the same 232 bits (bit 31..262). Both
layouts sum to exactly 232 bits.
"""
from __future__ import annotations

_2 = 2.0

SUBFRAME_BITS = 292        # TLM+TOWC+Alert+Autonav+SFID+Spare(30) + data(232)
                            # + CRC(24) + tail(6)
HEADER_BITS = 30            # TLM(8) TOWC(17) Alert(1) Autonav(1) SFID(2) Spare(1)
DATA_BITS = 232
CRC_BITS = 24
TAIL_BITS = 6
SYNC_WORD = 0xEB90          # 16 bits, uncoded, prepended after FEC+interleave
SYNC_BITS = 16
FEC_SYMBOLS = 584           # 292 bits * rate 1/2
INTERLEAVE_COLS = 73
INTERLEAVE_ROWS = 8

LAYOUT: dict[int, list[tuple[str, str, float, int]]] = {
    1: [
        ("wn",        "u", 1.0,       10),
        ("af0",       "s", _2 ** -31, 22),
        ("af1",       "s", _2 ** -43, 16),
        ("af2",       "s", _2 ** -55,  8),
        ("ura",       "u", 1.0,        4),
        ("toc",       "u", 16.0,      16),
        ("tgd",       "s", _2 ** -31,  8),
        ("delta_n",   "s", _2 ** -41, 22),
        ("iodec",     "u", 1.0,        8),
        ("reserved1", "u", 1.0,       10),
        ("l5_flag",   "u", 1.0,        1),
        ("s_flag",    "u", 1.0,        1),
        ("cuc",       "s", _2 ** -28, 15),
        ("cus",       "s", _2 ** -28, 15),
        ("cic",       "s", _2 ** -28, 15),
        ("cis",       "s", _2 ** -28, 15),
        ("crc",       "s", _2 ** -4,  15),
        ("crs",       "s", _2 ** -4,  15),
        ("idot",      "s", _2 ** -43, 14),
        ("spare1",    "u", 1.0,        2),
    ],
    2: [
        ("m0",        "s", _2 ** -31, 32),
        ("toe",       "u", 16.0,      16),
        ("e",         "u", _2 ** -33, 32),
        ("sqrt_a",    "u", _2 ** -19, 32),
        ("omega0",    "s", _2 ** -31, 32),
        ("omega",     "s", _2 ** -31, 32),
        ("omega_dot", "s", _2 ** -41, 22),
        ("i0",        "s", _2 ** -31, 32),
        ("spare2",    "u", 1.0,        2),
    ],
}

assert sum(n for _, _, _, n in LAYOUT[1]) == DATA_BITS
assert sum(n for _, _, _, n in LAYOUT[2]) == DATA_BITS
