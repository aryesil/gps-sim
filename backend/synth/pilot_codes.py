"""Secondary codes of the dataless pilot components.

Tables are copied from PocketSDR ``sdr_code.c`` / ``sdr_code_gal.c``, the
reference the primary codes were verified against:

* GPS / QZSS L5 Q5: NH20 (IS-GPS-705), one chip per 1 ms primary period.
* Galileo E5a-Q: CS100 memory codes, PRN n uses CS100_n (Galileo OS SIS
  ICD Table 18), one chip per 1 ms primary period.
* BeiDou B2a pilot: 100-chip truncated Weil codes of length 1021
  (BDS-SIS-ICD-B2a-1.0 Sec. 4.2.2), one chip per 1 ms primary period.

All returned as tuples of {-1, +1} with ICD bit 0 -> +1.
"""
from __future__ import annotations

import functools

# NH20 = 00000100110101001110
NH20 = tuple(1 if b == "0" else -1 for b in "00000100110101001110")

_CS100 = (
    "83F6F69D8F6E15411FB8C9B1C",
    "66558BD3CE0C7792E83350525",
    "59A025A9C1AF0651B779A8381",
    "D3A32640782F7B18E4DF754B7",
    "B91FCAD7760C218FA59348A93",
    "BAC77E933A779140F094FBF98",
    "537785DE280927C6B58BA6776",
    "EFCAB4B65F38531ECA22257E2",
    "79F8CAE838475EA5584BEFC9B",
    "CA5170FEA3A810EC606B66494",
    "1FC32410652A2C49BD845E567",
    "FE0A9A7AFDAC44E42CB95D261",
    "B03062DC2B71995D5AD8B7DBE",
    "F6C398993F598E2DF4235D3D5",
    "1BB2FB8B5BF24395C2EF3C5A1",
    "2F920687D238CC7046EF6AFC9",
    "34163886FC4ED7F2A92EFDBB8",
    "66A872CE47833FB2DFD5625AD",
    "99D5A70162C920A4BB9DE1CA8",
    "81D71BD6E069A7ACCBEDC66CA",
    "A654524074A9E6780DB9D3EC6",
    "C3396A101BEDAF623CFC5BB37",
    "C3D4AB211DF36F2111F2141CD",
    "3DFF25EAE761739265AF145C1",
    "994909E0757D70CDE389102B5",
    "B938535522D119F40C25FDAEC",
    "C71AB549C0491537026B390B7",
    "0CDB8C9E7B53F55F5B0A0597B",
    "61C5FA252F1AF81144766494F",
    "626027778FD3C6BB4BAA7A59D",
    "E745412FF53DEBD03F1C9A633",
    "3592AC083F3175FA724639098",
    "52284D941C3DCAF2721DDB1FD",
    "73B3D8F0AD55DF4FE814ED890",
    "94BF16C83BD7462F6498E0282",
    "A8C3DE1AC668089B0B45B3579",
    "E23FFC2DD2C14388AD8D6BEC8",
    "F2AC871CDF89DDC06B5960D2B",
    "06191EC1F622A77A526868BA1",
    "22D6E2A768E5F35FFC8E01796",
    "25310A06675EB271F2A09EA1D",
    "9F7993C621D4BEC81A0535703",
    "D62999EACF1C99083C0B4A417",
    "F665A7EA441BAA4EA0D01078C",
    "46F3D3043F24CDEABD6F79543",
    "E2E3E8254616BD96CEFCA651A",
    "E548231A82F9A01A19DB5E1B2",
    "265C7F90A16F49EDE2AA706C8",
    "364A3A9EB0F0481DA0199D7EA",
    "9810A7A898961263A0F749F56",
)

# B2a pilot secondary: Weil phase difference w and truncation point p per PRN
_B2AS_PH_DIFF = (
    123, 55, 40, 139, 31, 175, 350, 450, 478, 8, 73, 97,
    213, 407, 476, 4, 15, 47, 163, 280, 322, 353, 375, 510,
    332, 7, 13, 16, 18, 25, 50, 81, 118, 127, 132, 134,
    164, 177, 208, 249, 276, 349, 439, 477, 498, 88, 155, 330,
    3, 21, 84, 111, 128, 153, 197, 199, 214, 256, 265, 291,
    324, 326, 340,
)
_B2AS_TRUNC_PNT = (
    138, 570, 351, 77, 885, 247, 413, 180, 3, 26, 17, 172,
    30, 1008, 646, 158, 170, 99, 53, 179, 925, 114, 10, 584,
    60, 3, 684, 263, 545, 22, 546, 190, 303, 234, 38, 822,
    57, 668, 697, 93, 18, 66, 318, 133, 98, 70, 132, 26,
    354, 58, 41, 182, 944, 205, 23, 1, 792, 641, 83, 7,
    111, 96, 92,
)


def _hex_chips(h: str, n: int) -> tuple[int, ...]:
    bits = "".join(f"{int(c, 16):04b}" for c in h)[:n]
    return tuple(1 if b == "0" else -1 for b in bits)


def e5aq_secondary(prn: int) -> tuple[int, ...]:
    """CS100_prn, PRN 1..50."""
    if not 1 <= prn <= 50:
        raise ValueError(f"E5a-Q secondary: PRN {prn} out of range")
    return _hex_chips(_CS100[prn - 1], 100)


@functools.lru_cache(maxsize=1)
def _legendre_1021() -> tuple[int, ...]:
    n = 1021
    seq = [1] * n
    for i in range(1, n):
        seq[(i * i) % n] = -1
    return tuple(seq)


def b2ap_secondary(prn: int) -> tuple[int, ...]:
    """100-chip B2a pilot secondary, PRN 1..63."""
    if not 1 <= prn <= 63:
        raise ValueError(f"B2a pilot secondary: PRN {prn} out of range")
    leg = _legendre_1021()
    w = _B2AS_PH_DIFF[prn - 1]
    p = _B2AS_TRUNC_PNT[prn - 1]
    out = []
    for i in range(100):
        k = (i + p - 1) % 1021
        out.append(leg[k] * leg[(k + w) % 1021])
    return tuple(out)
