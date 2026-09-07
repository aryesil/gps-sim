"""CRC-24Q (polynomial 0x1864CFB), the parity used by GPS CNAV, Galileo,
SBAS and RTCM. MSB-first over the message bits, with 24 trailing zero
shifts (the "augmented message" form). Shared by the navigation-message
encoders / decoders so the algorithm is defined once.
"""
from __future__ import annotations

_CRC24Q = 0x1864CFB


def crc24q(bits) -> list[int]:
    reg = 0
    for b in bits:
        reg ^= (int(b) & 1) << 23
        reg <<= 1
        if reg & (1 << 24):
            reg ^= _CRC24Q
        reg &= 0xFFFFFF
    for _ in range(24):
        reg <<= 1
        if reg & (1 << 24):
            reg ^= _CRC24Q
        reg &= 0xFFFFFF
    return [(reg >> (23 - i)) & 1 for i in range(24)]
