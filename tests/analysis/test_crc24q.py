"""CRC-24Q verification tests for Galileo I/NAV and shared _crc module."""
from __future__ import annotations

import random
import pytest

from backend.analysis import _crc
from backend.analysis import inav_encode


def test_crc24q_standard_ascii_vector():
    """Standart kontrol vektörü: ASCII '123456789' -> 0xCDE703 (CRC-24/Q, init 0, xorout 0)."""
    ascii_bytes = b"123456789"
    bits = [(b >> (7 - i)) & 1 for b in ascii_bytes for i in range(8)]

    # 1. _crc.crc24q (tablo versiyonu)
    crc_table = _crc.crc24q(bits)
    val_table = int("".join(map(str, crc_table)), 2)
    assert hex(val_table) == "0xcde703"

    # 2. inav_encode._crc24q (bit-by-bit doğrudan form)
    crc_inav = inav_encode._crc24q(bits)
    val_inav = int("".join(map(str, crc_inav)), 2)
    assert hex(val_inav) == "0xcde703"

    # İkisi birebir aynı olmalı
    assert crc_table == crc_inav


def test_crc24q_random_196bit_equivalence():
    """Rastgele 196 bitlik I/NAV veri girdisi için inav_encode._crc24q(x) == _crc.crc24q(x)."""
    rng = random.Random(42)
    for _ in range(50):
        bits = [rng.randint(0, 1) for _ in range(196)]
        assert inav_encode._crc24q(bits) == _crc.crc24q(bits)
