"""Shared F/NAV message field layouts (Galileo OS SIS ICD Annex B, word
types 1-4 -- clock/iono/BGD, and ephemeris parts 1-3).

One ordered ``(name, kind, scale, nbits)`` list per word type, walked
identically by ``fnav_encode`` (physical value -> bits) and ``fnav_decode``
(bits -> physical value) so the two never drift, the same shared-table
pattern as ``_cnav_layout.py``.

``kind``: ``"u"`` unsigned, ``"s"`` two's-complement signed. Angular fields
are carried in **semicircles** (multiply by pi for radians) -- scale
factors below are the raw semicircle LSBs, matching ``_cnav_layout.py``'s
convention; the pi multiply happens in ``fnav_decode.reconstruct_ephemeris``
/ the encoder call sites, not in this table. ``"_reserved"`` entries are
ICD reserved/SISA/iono/almanac-adjacent bits not needed for a position fix
(this project's civil-band phases transmit type 1-4 only; almanac word
types 5/6 are out of scope) -- the encoder writes them zero and the
decoder skips them by width alone.

Field bit positions/widths/scale-factors are transcribed from GNSS-SDR
src/core/system_parameters/Galileo_FNAV.h (fetched 2026-09-13), which cites
the Galileo OS SIS ICD directly -- unlike ``_cnav_layout.py`` (IS-GPS-200
table transcribed without the document in hand), these track a primary
source. The CRC scope and per-page framing (below) are this project's own
choice: each page independently CRCs and FEC-flushes rather than the
continuous-stream framing real hardware uses -- see ``fnav_encode``'s
docstring.
"""
from __future__ import annotations

_2 = 2.0

MSG_BITS = 214             # CRC-covered message (6-bit type + 208 data)
CRC_BITS = 24
TAIL_BITS = 6               # conv-encoder flush (this project's per-page choice)
PAGE_SYMS = 500             # 10 s nominal page @ 50 sym/s
PREAMBLE_SYMS = 12          # uncoded
CODED_SYMS = 488            # 8 x 61 interleaver -- FEC(244 bits) -> 488 symbols

LAYOUT: dict[int, list[tuple[str, str, float, int]]] = {
    1: [   # SVID, clock correction, SISA, iono, BGD, GST, health
        ("type",      "u", 1.0,        6),
        ("prn",       "u", 1.0,        6),
        ("iodnav",    "u", 1.0,       10),
        ("toc",       "u", 60.0,      14),
        ("af0",       "s", _2 ** -34, 31),
        ("af1",       "s", _2 ** -46, 21),
        ("af2",       "s", _2 ** -59,  6),
        ("_reserved", "u", 0.0,        8),   # SISA
        ("_reserved", "u", 0.0,       11),   # ai0 (iono NeQuick)
        ("_reserved", "u", 0.0,       11),   # ai1
        ("_reserved", "u", 0.0,       14),   # ai2
        ("_reserved", "u", 0.0,        5),   # iono region flags 1-5
        ("tgd",       "s", _2 ** -32, 10),   # BGD(E1, E5a)
        ("_reserved", "u", 0.0,        2),   # E5a signal health status
        ("week",      "u", 1.0,       12),   # GST week (project-wide `week`)
        ("tow",       "u", 1.0,       20),   # GST TOW
        ("_reserved", "u", 0.0,        1),   # E5a data validity status
        ("_reserved", "u", 0.0,       26),   # spare
    ],
    2: [   # Ephemeris 1/3 + GST
        ("type",       "u", 1.0,        6),
        ("iodnav",     "u", 1.0,       10),
        ("m0",         "s", _2 ** -31, 32),
        ("omega_dot",  "s", _2 ** -43, 24),
        ("e",          "u", _2 ** -33, 32),
        ("sqrtA",      "u", _2 ** -19, 32),
        ("omega0",     "s", _2 ** -31, 32),
        ("idot",       "s", _2 ** -43, 14),
        ("week",       "u", 1.0,       12),
        ("tow",        "u", 1.0,       20),
    ],
    3: [   # Ephemeris 2/3 + GST
        ("type",      "u", 1.0,        6),
        ("iodnav",    "u", 1.0,       10),
        ("i0",        "s", _2 ** -31, 32),
        ("omega",     "s", _2 ** -31, 32),
        ("delta_n",   "s", _2 ** -43, 16),
        ("cuc",       "s", _2 ** -29, 16),
        ("cus",       "s", _2 ** -29, 16),
        ("crc",       "s", _2 ** -5,  16),
        ("crs",       "s", _2 ** -5,  16),
        ("toe",       "u", 60.0,      14),
        ("week",      "u", 1.0,       12),
        ("tow",       "u", 1.0,       20),
        ("_reserved", "u", 0.0,        8),
    ],
    4: [   # Ephemeris 3/3 + GST-UTC / GST-GPS conversion (conversion terms unused here)
        ("type",      "u", 1.0,        6),
        ("iodnav",    "u", 1.0,       10),
        ("cic",       "s", _2 ** -29, 16),
        ("cis",       "s", _2 ** -29, 16),
        ("_reserved", "u", 0.0,       32),   # A0 (GST-UTC)
        ("_reserved", "u", 0.0,       24),   # A1 (GST-UTC)
        ("_reserved", "u", 0.0,        8),   # dt_LS
        ("_reserved", "u", 0.0,        8),   # t0t
        ("_reserved", "u", 0.0,        8),   # WNot
        ("_reserved", "u", 0.0,        8),   # WNlsf
        ("_reserved", "u", 0.0,        3),   # DN
        ("_reserved", "u", 0.0,        8),   # dt_LSF
        ("_reserved", "u", 0.0,        8),   # t0g (GST-GPS)
        ("_reserved", "u", 0.0,       16),   # A0g
        ("_reserved", "u", 0.0,       12),   # A1g
        ("_reserved", "u", 0.0,        6),   # WN0g
        ("tow",       "u", 1.0,       20),
        ("_reserved", "u", 0.0,        5),   # spare
    ],
}

CYCLE = (1, 2, 3, 4)
