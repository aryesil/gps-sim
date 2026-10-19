"""OSNMA SIS ICD 1.1 constants, enums and lookup tables."""
from __future__ import annotations

from enum import IntEnum

KS_BITS = (96, 104, 112, 120, 128, 160, 192, 224, 256)
# ICD 1.1 Table 11: TS codes 0-4 are Reserved (the 10..18 bit tags are Test Phase ICD 1.0 only).
TS_BITS = (None, None, None, None, None, 20, 24, 28, 32, 40)
NB_DK_BLOCKS = {1: 7, 2: 8, 3: 9, 4: 10, 5: 11, 6: 12, 7: 13, 8: 14}
NB_DP_BLOCKS = {7: 13, 8: 14, 9: 15, 10: 16}


class NMAS(IntEnum):
    RESERVED = 0
    TEST = 1
    OPERATIONAL = 2
    DONT_USE = 3


class CPKS(IntEnum):
    RESERVED = 0
    NOMINAL = 1
    EOC = 2
    CREV = 3
    NPK = 4
    PKREV = 5
    NMT = 6
    AM = 7


class HF(IntEnum):
    SHA256 = 0
    # 1 is Reserved in ICD 1.1 Table 8 (SHA3-224 appears only in OSNMAlib)
    SHA3_256 = 2


class MF(IntEnum):
    HMAC_SHA256 = 0
    CMAC_AES = 1


class NPKT(IntEnum):
    RESERVED = 0
    ECDSA_P256 = 1
    # 2 is Reserved
    ECDSA_P521 = 3
    OAM = 4


# ICD 1.1 Annex C Table 16. First tuple applies when gst_sf % 60 == 0 (MACK in the first
# 30 s of a GST minute), second otherwise. "FLX" = flexible slot (Tag-Info per ICD 6.6).
_S = lambda t: tuple(t.replace(",", " ").split())
MACLT: dict[int, tuple[tuple[str, ...], tuple[str, ...]]] = {
    27: (_S("00S 00E 00E 00E 12S 00E"), _S("00S 00E 00E 04S 12S 00E")),
    28: (_S("00S 00E 00E 00E 00S 00E 00E 12S 00E 00E"),
         _S("00S 00E 00E 00S 00E 00E 04S 12S 00E 00E")),
    31: (_S("00S 00E 00E 12S 00E"), _S("00S 00E 00E 12S 04S")),
    33: (_S("00S 00E 04S 00E 12S 00E"), _S("00S 00E 00E 12S 00E 12E")),
    34: (_S("00S FLX 04S FLX 12S 00E"), _S("00S FLX 00E 12S 00E 12E")),
    35: (_S("00S FLX 04S FLX 12S FLX"), _S("00S FLX FLX 12S FLX FLX")),
    36: (_S("00S FLX 04S FLX 12S"), _S("00S FLX 00E 12S 12E")),
    37: (_S("00S 00E 04S 00E 12S"), _S("00S 00E 00E 12S 12E")),
    38: (_S("00S FLX 04S FLX 12S"), _S("00S FLX FLX 12S FLX")),
    39: (_S("00S FLX 04S FLX"), _S("00S FLX 00E 12S")),
    40: (_S("00S 00E 04S 12S"), _S("00S 00E 00E 12E")),
    41: (_S("00S FLX 04S FLX"), _S("00S FLX FLX 12S")),
}

ADKD0_MASK = {1: (6, 126), 2: (6, 126), 3: (6, 128), 4: (6, 126), 5: (6, 73)}
ADKD4_MASK = {6: (6, 105), 10: (86, 128)}
NAVDATA_LEN = {0: 549, 4: 141, 12: 549}

DSM_BLOCK_BITS = 104
HKROOT_BITS = 120
MACK_BITS = 480


def nt_for(ks_bits: int, ts_bits: int) -> int:
    return (MACK_BITS - ks_bits) // (ts_bits + 16)


def code_of(table: tuple, value: int) -> int:
    try:
        return table.index(value)
    except ValueError:
        raise ValueError(f"{value} not in {table}") from None
