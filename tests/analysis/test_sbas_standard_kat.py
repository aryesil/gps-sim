"""RTCA DO-229 / ICAO Annex 10 SBAS L1 Standart Uyum ve Bilinen Yanıt Testleri (KAT).

Bu test süiti, gerçek havacılık alıcılarının (u-blox, RTKLIB vb.) SBAS L1 sinyalini
doğrulamada kullandığı standart parametreleri ve bit ofsetlerini (Tablo A-8) bağımsız
olarak doğrular.
"""
from __future__ import annotations

import math
import pytest
import numpy as np

from backend.analysis import sbas_iono, sbas_decode, sbas_encode, _crc, _viterbi


def test_do229_table_a8_bit_offsets_and_scalings():
    """RTCA DO-229 Tablo A-8 Mesaj Tipi 9 bit ofsetleri, bit uzunlukları ve ölçekleme faktörleri:
    - IODN: bit 0..7 (8 bit)
    - t0,GEO: bit 8..20 (13 bit, LSB = 16 s, aralık 0..5399)
    - URAI: bit 21..24 (4 bit, 0..15)
    - X_G: bit 25..54 (30 bit, LSB = 0.08 m)
    - Y_G: bit 55..84 (30 bit, LSB = 0.08 m)
    - Z_G: bit 85..109 (25 bit, LSB = 0.4 m)
    - VX_G: bit 110..126 (17 bit, LSB = 0.000625 m/s)
    - VY_G: bit 127..143 (17 bit, LSB = 0.000625 m/s)
    - VZ_G: bit 144..161 (18 bit, LSB = 0.004 m/s)
    - AX_G: bit 162..171 (10 bit, LSB = 0.0000125 m/s^2)
    - AY_G: bit 172..181 (10 bit, LSB = 0.0000125 m/s^2)
    - AZ_G: bit 182..191 (10 bit, LSB = 0.0000625 m/s^2)
    - af0: bit 192..203 (12 bit, LSB = 2^-31 s)
    - af1: bit 204..211 (8 bit, LSB = 2^-40 s/s)
    Toplam = 212 bit.
    """
    sample_eph = {
        "iodn": 42,
        "toe_ref": 43200.0,       # t0 = 43200 s -> 43200/16 = 2700
        "urai": 0,                # Healthy GEO (sigma <= 2.4m)
        "x_m": -24000000.0,       # ~3.5e7 - 4.5e7 GEO menzili
        "y_m": 34000000.0,
        "z_m": 1200000.0,
        "vx": 1.25,
        "vy": -2.5,
        "vz": 0.064,
        "ax": 0.000025,
        "ay": -0.0000125,
        "az": 0.000125,
        "af0": -1.5e-7,
        "af1": 2.0e-11,
    }

    bits = sbas_iono.encode_type9(sample_eph, tow_s=43200.0)
    assert len(bits) == 212

    # 1. IODN kontrolü (bit 0..7)
    iodn_val = int("".join(map(str, bits[0:8])), 2)
    assert iodn_val == 42

    # 2. t0 kontrolü (bit 8..20)
    t0_cnt = int("".join(map(str, bits[8:21])), 2)
    assert t0_cnt == 2700
    assert t0_cnt * 16.0 == 43200.0

    # 3. URAI kontrolü (bit 21..24)
    urai_val = int("".join(map(str, bits[21:25])), 2)
    assert urai_val == 0

    # 4. Kod çözücü doğrulaması (parse_type9)
    parsed = sbas_decode.parse_type9(bits)
    assert parsed["iodn"] == 42
    assert parsed["t0"] == 43200.0
    assert parsed["urai"] == 0
    assert abs(parsed["x_m"] - sample_eph["x_m"]) < 0.08
    assert abs(parsed["y_m"] - sample_eph["y_m"]) < 0.08
    assert abs(parsed["z_m"] - sample_eph["z_m"]) < 0.4
    assert abs(parsed["vx"] - sample_eph["vx"]) < 0.000625
    assert abs(parsed["vy"] - sample_eph["vy"]) < 0.000625
    assert abs(parsed["vz"] - sample_eph["vz"]) < 0.004
    assert abs(parsed["ax"] - sample_eph["ax"]) < 0.0000125
    assert abs(parsed["ay"] - sample_eph["ay"]) < 0.0000125
    assert abs(parsed["az"] - sample_eph["az"]) < 0.0000625
    assert abs(parsed["af0"] - sample_eph["af0"]) < 2.0 ** -31
    assert abs(parsed["af1"] - sample_eph["af1"]) < 2.0 ** -40


def test_standard_crc24q_known_vector_and_properties():
    """DO-229 doğrudan form CRC-24Q doğruluk testi."""
    ascii_bytes = b"123456789"
    bits = [(b >> (7 - i)) & 1 for b in ascii_bytes for i in range(8)]
    crc = _crc.crc24q(bits)
    val = int("".join(map(str, crc)), 2)
    assert hex(val) == "0xcde703"

    # g(x) bölünebilirlik kuralı
    codeword = bits + crc
    assert _crc.crc24q(codeword) == [0] * 24
    assert _crc.check_crc24q(codeword, data_bits=len(bits), crc_bits=24) is True


def test_standard_fec_impulse_response_and_viterbi():
    """DO-229 Bölüm A.4.1.1 / CCSDS standart K=7 r=1/2 polinomları darbe yanıtı.
    G1 = 171_8 = [1, 1, 1, 1, 0, 0, 1]
    G2 = 133_8 = [1, 0, 1, 1, 0, 1, 1]
    """
    conv = sbas_encode._Conv()
    coded = conv.encode([1, 0, 0, 0, 0, 0, 0])
    g1_out = [coded[2 * i] for i in range(7)]
    g2_out = [coded[2 * i + 1] for i in range(7)]
    assert g1_out == [1, 1, 1, 1, 0, 0, 1]
    assert g2_out == [1, 0, 1, 1, 0, 1, 1]

    # Viterbi tersleme doğrulaması
    data = [1, 0, 1, 1, 0, 0, 1, 0] * 20
    conv2 = sbas_encode._Conv()
    coded2 = conv2.encode(data)
    decoded = _viterbi.viterbi_free_decode(coded2, invert_g2=False)
    assert decoded == data
