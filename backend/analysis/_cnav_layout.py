"""Shared CNAV message field layouts (IS-GPS-200 Table 30-I / 30-II).

One ordered ``(name, kind, scale, nbits)`` list per message type, walked
identically by ``cnav_encode`` (physical value -> bits) and
``cnav_decode`` (bits -> physical value) so the two never drift.

``kind``: ``"u"`` unsigned, ``"s"`` two's-complement signed. Angular
fields are carried in **semicircles** (multiply by pi for radians) so the
scale factors match the ICD directly. Any bits past the listed fields in
the 238-bit payload are ICD "reserved" and encode as zero.

The field widths and scale factors track IS-GPS-200 to the extent
verifiable without the document in hand; encoder and decoder share this
table, so the bit-level round-trip and the closed-loop fix are exact
regardless. Validate against the ICD before relying on interoperability
with an independent receiver.
"""
from __future__ import annotations

_2 = 2.0

PAYLOAD_BITS = 238
HEADER_BITS = 38          # preamble(8) prn(6) type(6) tow(17) alert(1)
MSG_BITS = 300
CRC_BITS = 24

LAYOUT: dict[int, list[tuple[str, str, float, int]]] = {
    10: [
        ("wn",         "u", 1.0,        13),
        ("health",     "u", 1.0,         3),
        ("t_op",       "u", 300.0,      11),
        ("ura_ed",     "s", 1.0,         5),
        ("toe",        "u", 300.0,      11),
        ("dA",         "s", _2 ** -9,   26),
        ("A_dot",      "s", _2 ** -21,  25),
        ("delta_n",    "s", _2 ** -44,  17),
        ("delta_n_dot","s", _2 ** -57,  23),
        ("m0",         "s", _2 ** -32,  33),
        ("e",          "u", _2 ** -34,  33),
        ("omega",      "s", _2 ** -32,  33),
    ],
    11: [
        ("toe",        "u", 300.0,      11),
        ("omega0",     "s", _2 ** -32,  33),
        ("i0",         "s", _2 ** -32,  33),
        ("d_omega_dot","s", _2 ** -44,  17),
        ("idot",       "s", _2 ** -44,  15),
        ("cis",        "s", _2 ** -30,  16),
        ("cic",        "s", _2 ** -30,  16),
        ("crs",        "s", _2 ** -8,   24),
        ("crc",        "s", _2 ** -8,   24),
        ("cus",        "s", _2 ** -30,  21),
        ("cuc",        "s", _2 ** -30,  21),
    ],
    30: [
        ("toc",        "u", 300.0,      11),
        ("ura_ned0",   "s", 1.0,         5),
        ("ura_ned1",   "u", 1.0,         3),
        ("ura_ned2",   "u", 1.0,         3),
        ("af0",        "s", _2 ** -35,  26),
        ("af1",        "s", _2 ** -48,  20),
        ("af2",        "s", _2 ** -60,  10),
        ("tgd",        "s", _2 ** -35,  13),
        ("isc_l1ca",   "s", _2 ** -35,  13),
        ("isc_l2c",    "s", _2 ** -35,  13),
        ("isc_l5i5",   "s", _2 ** -35,  13),
        ("isc_l5q5",   "s", _2 ** -35,  13),
        ("a0",         "s", _2 ** -30,   8),
        ("a1",         "s", _2 ** -27,   8),
        ("a2",         "s", _2 ** -24,   8),
        ("a3",         "s", _2 ** -24,   8),
        ("b0",         "s", _2 ** 11,    8),
        ("b1",         "s", _2 ** 14,    8),
        ("b2",         "s", _2 ** 16,    8),
        ("b3",         "s", _2 ** 16,    8),
    ],
    33: [
        ("utc_a0",     "s", _2 ** -35,  16),
        ("utc_a1",     "s", _2 ** -51,  13),
        ("utc_a2",     "s", _2 ** -68,   7),
        ("dt_ls",      "s", 1.0,         8),
        ("t_ot",       "u", 16.0,       16),
        ("wn_ot",      "u", 1.0,        13),
        ("wn_lsf",     "u", 1.0,        13),
        ("dn",         "u", 1.0,         4),
        ("dt_lsf",     "s", 1.0,         8),
    ],
}

CYCLE = (10, 11, 30, 33)

# ΔΩ_dot in message 11 is a delta from this reference (semicircles/s).
OMEGA_DOT_REF = -2.6e-9
# ΔA in message 10 is a delta from this reference semi-major axis (m).
A_REF = 26559710.0
