"""OSNMA SIS ICD 1.1 constants, enums and lookup tables."""
from __future__ import annotations

from enum import IntEnum

KS_BITS = (96, 104, 112, 120, 128, 160, 192, 224, 256)
TS_BITS = (10, 12, 14, 16, 18, 20, 24, 28, 32, 40)
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
    SHA3_224 = 1
    SHA3_256 = 2


class MF(IntEnum):
    HMAC_SHA256 = 0
    CMAC_AES = 1


class NPKT(IntEnum):
    RESERVED = 0
    ECDSA_P256 = 1
    ECDSA_P521 = 2
    OAM = 4


# first tuple applies when gst_sf % 60 == 0, second otherwise
MACLT: dict[int, tuple[tuple[str, ...], tuple[str, ...]]] = {
    33: (("00S", "00E", "04S", "00E", "12S", "00E"),
         ("00S", "00E", "00E", "12S", "00E", "12E")),
    34: (("00S", "00E", "12S", "00E", "04S", "00E"),
         ("00S", "00E", "00E", "12S", "00E", "12E")),
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
