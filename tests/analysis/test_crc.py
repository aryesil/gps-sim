"""Standard conformance and equivalence tests for CRC-24Q."""
from __future__ import annotations

import random
import pytest

from backend.analysis import _crc
from backend.analysis import sbas_encode


def test_crc24q_standard_ascii_vector():
    """Doğrulanmış standart test vektörü: '123456789' ASCII -> 0xCDE703."""
    ascii_bytes = b"123456789"
    bits = [(b >> (7 - i)) & 1 for b in ascii_bytes for i in range(8)]
    
    # 1. sbas_encode.crc24q
    crc_sbas = sbas_encode.crc24q(bits)
    val_sbas = int("".join(map(str, crc_sbas)), 2)
    assert hex(val_sbas) == "0xcde703"

    # 2. _crc.crc24q (hızlandırılmış tablo versiyonu)
    crc_table = _crc.crc24q(bits)
    val_table = int("".join(map(str, crc_table)), 2)
    assert hex(val_table) == "0xcde703"


def test_crc24q_equivalence_on_random_sbas_blocks():
    """sbas_encode.crc24q ile _crc.crc24q'nin rastgele 226-bit SBAS bloklarında birebir eşitliği."""
    rng = random.Random(42)
    for _ in range(50):
        bits = [rng.randint(0, 1) for _ in range(226)]
        assert sbas_encode.crc24q(bits) == _crc.crc24q(bits)


def test_crc24q_divisibility_zero_remainder():
    """CRC-24Q g(x) bölünebilirlik özelliği: payload + CRC-24Q'nun CRC'si 0 olmalıdır."""
    rng = random.Random(1234)
    for _ in range(20):
        bits = [rng.randint(0, 1) for _ in range(226)]
        crc = _crc.crc24q(bits)
        codeword = bits + crc
        # Codeword üzerinden doğrudan crc hesaplandığında kalan 0 olmalıdır:
        rem_table = _crc.crc24q(codeword)
        assert rem_table == [0] * 24
        rem_sbas = sbas_encode.crc24q(codeword)
        assert rem_sbas == [0] * 24
        assert _crc.check_crc24q(codeword, data_bits=226, crc_bits=24) is True
