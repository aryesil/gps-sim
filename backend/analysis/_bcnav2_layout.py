"""Shared BeiDou B-CNAV2 (B2a) 288-info-bit field layout, real bit-offsets
and scale factors from GNSS-SDR's real, merged B-CNAV2 decoder
(``beidou_cnav2_navigation_message.cc``, PR #1093 branch ``next``,
GPL-3.0-or-later) -- itself implementing BDS-SIS-ICD-B2a-1.0 Section 6.

Every message type shares a 30-bit common header (PRN 6b, message type 6b,
SOW 18b @ 3 s LSB) and a 24-bit trailing CRC-24Q (bits 264..288), leaving
234 bits (30..264) for message-specific fields. The bit ranges below are
NOT contiguous within that window -- there are real gaps (health bits,
BDGIM ionospheric coefficients, reserved bits) whose exact content this
project does not need and does not encode; both the encoder and decoder
touch only the offsets listed here, so unlisted bits are simply zero and
never read back.

Each field tuple is ``(name, offset, nbits, kind, scale)``: ``offset``/
``nbits`` are absolute bit positions in the 288-bit info block, ``kind``
is ``"u"`` (unsigned) or ``"s"`` (two's-complement signed), and ``scale``
is the physical-unit LSB (already includes the ICD's ``* pi`` for angle
fields; ``None`` for raw integer fields like IODE/IODC/sat_type).
"""
from __future__ import annotations

import math

_PI = math.pi
TWO_N8 = 2.0**-8
TWO_N9 = 2.0**-9
TWO_N21 = 2.0**-21
TWO_N30 = 2.0**-30
TWO_N32 = 2.0**-32
TWO_N34 = 2.0**-34
TWO_N44 = 2.0**-44
TWO_N50 = 2.0**-50
TWO_N57 = 2.0**-57
TWO_N66 = 2.0**-66

MSG_EPH1 = 10
MSG_EPH2 = 11
MSG_CLK_IONO = 30

A_REF_MEO = 27906100.0
A_REF_IGSO = 42162200.0

# Common 30-bit header, every message type.
HEADER = [
    ("prn", 0, 6, "u", None),
    ("mes_type", 6, 6, "u", None),
    ("sow", 12, 18, "u", 3.0),
]

# MT10 -- Ephemeris I (BDS-SIS-ICD-B2a-1.0 Table 6-13/6-14).
MT10_FIELDS = [
    ("wn", 30, 13, "u", None),
    ("iode", 53, 8, "u", None),
    ("toe", 61, 11, "u", 300.0),
    ("sat_type", 72, 2, "u", None),
    ("delta_a", 74, 26, "s", TWO_N9),
    ("a_dot", 100, 25, "s", TWO_N21),
    ("delta_n0", 125, 17, "s", TWO_N44 * _PI),
    ("delta_n0_dot", 142, 23, "s", TWO_N57 * _PI),
    ("m0", 165, 33, "s", TWO_N32 * _PI),
    ("e", 198, 33, "u", TWO_N34),
    ("omega", 231, 33, "s", TWO_N32 * _PI),
]

# MT11 -- Ephemeris II (BDS-SIS-ICD-B2a-1.0 Table 6-15/6-16).
MT11_FIELDS = [
    ("hs", 30, 2, "u", None),
    ("omega0", 42, 33, "s", TWO_N32 * _PI),
    ("i0", 75, 33, "s", TWO_N32 * _PI),
    ("omega_dot", 108, 19, "s", TWO_N44 * _PI),
    ("idot", 127, 15, "s", TWO_N44 * _PI),
    ("cis", 142, 16, "s", TWO_N30),
    ("cic", 158, 16, "s", TWO_N30),
    ("crs", 174, 24, "s", TWO_N8),
    ("crc", 198, 24, "s", TWO_N8),
    ("cus", 222, 21, "s", TWO_N30),
    ("cuc", 243, 21, "s", TWO_N30),
]

# MT30 -- Clock / iono / group delay (BDS-SIS-ICD-B2a-1.0 Table 6-19..6-21).
# Iono (BDGIM) and TGD_B1Cp fields are not encoded -- unused by the B2a-only
# fix path in this project.
MT30_FIELDS = [
    ("hs", 30, 2, "u", None),
    ("toc", 42, 11, "u", 300.0),
    ("af0", 53, 25, "s", TWO_N34),
    ("af1", 78, 22, "s", TWO_N50),
    ("af2", 100, 11, "s", TWO_N66),
    ("iodc", 111, 10, "u", None),
    ("tgd_b2ap", 121, 12, "s", TWO_N34),
    ("isc_b2ad", 133, 12, "s", TWO_N34),
]

FIELDS_BY_TYPE = {
    MSG_EPH1: MT10_FIELDS,
    MSG_EPH2: MT11_FIELDS,
    MSG_CLK_IONO: MT30_FIELDS,
}


def a_ref_for(sat_type: int) -> float:
    """IGSO reference semi-major axis for sat_type in {1, 2}, else MEO --
    matches GNSS-SDR's ``a_ref_from_sat_type`` verbatim."""
    return A_REF_IGSO if int(sat_type) in (1, 2) else A_REF_MEO
