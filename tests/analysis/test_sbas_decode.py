"""Unit tests for SBAS L1 GEO navigation message decoder (backend.analysis.sbas_decode).

Follows RTCA DO-229 / ICAO Annex 10 standards:
- 250 bps data, rate-1/2 K=7 convolutional code (G1=0o171, G2=0o133)
- Preamble rotation (0x53, 0x9A, 0xC6) and CRC-24Q verification
- Message Type 9 (GEO Navigation Message) parsing and ephemeris reconstruction
- 500 sym/s symbol stream decoding and IQ demodulation
"""
from __future__ import annotations

import math
import numpy as np
import pytest

from backend.analysis import sbas_decode as D
from backend.analysis import sbas_encode as S
from backend.synth import _lib


@pytest.fixture
def sample_sbas_eph():
    return {
        "system": "S",
        "prn": 120,
        "x_m": -24000000.0,
        "y_m": 34000000.0,
        "z_m": 1200000.0,
        "vx": 1.25,
        "vy": -5.0,
        "vz": 0.125,
        "ax": 0.00001,
        "ay": -0.00002,
        "az": 0.000015,
        "toe_ref": 561600.0,
        "tau": 1.5e-8,
        "gamma": 2.5e-12,
        "af0": 1.5e-8,
        "af1": 2.5e-12,
    }


def test_crc24q_verification_and_corruption(sample_sbas_eph):
    blk = S.build_block(0, 9, sample_sbas_eph)
    assert len(blk) == 250
    assert D.check_crc(blk) is True

    # Corrupt single bit
    blk_corrupted = list(blk)
    blk_corrupted[42] ^= 1
    assert D.check_crc(blk_corrupted) is False

    # Corrupt CRC field
    blk_bad_crc = list(blk)
    blk_bad_crc[235] ^= 1
    assert D.check_crc(blk_bad_crc) is False


def test_viterbi_free_and_viterbi_decode():
    conv = S._Conv()
    data = [1, 0, 1, 1, 0, 0, 1, 0] * 12
    coded = conv.encode(data)
    assert len(coded) == 2 * len(data)

    dec_free = D.viterbi_free_decode(coded)
    assert dec_free == data

    dec_hard = D.viterbi_decode(coded)
    assert dec_hard == data


def test_viterbi_corrects_single_bit_error():
    conv = S._Conv()
    data = [1, 0, 0, 1, 1, 0, 1, 0, 0, 1, 1, 1] * 8
    coded = conv.encode(data)

    # Invert a single bit in the coded stream
    coded_err = list(coded)
    coded_err[25] ^= 1

    dec = D.viterbi_decode(coded_err)
    assert dec == data


def test_decode_messages_from_stream(sample_sbas_eph):
    stream, rate = S.nav_stream(sample_sbas_eph, 0, 561600.0, 6)
    assert rate == 500.0

    blks = D.decode_messages(stream)
    assert len(blks) >= 6
    assert [b["preamble"] for b in blks[:3]] == [0x53, 0x9A, 0xC6]
    assert all(b["crc_ok"] for b in blks)
    assert blks[0]["type"] == 9


def test_decode_messages_with_offset_and_polarity_flip(sample_sbas_eph):
    stream, rate = S.nav_stream(sample_sbas_eph, 0, 561600.0, 6)
    sym01 = [1 if s > 0 else 0 for s in stream]

    # Prepend 13 arbitrary symbols and invert polarity
    shifted = [0] * 13 + [s ^ 1 for s in sym01]

    blks = D.decode_messages(shifted)
    assert len(blks) >= 5
    assert any(b["type"] == 9 and b["crc_ok"] for b in blks)

    rec = D.reconstruct_ephemeris(blks, prn=120)
    assert abs(rec["x_m"] - sample_sbas_eph["x_m"]) < 0.1


def test_reconstruct_ephemeris_accuracy(sample_sbas_eph):
    stream, _ = S.nav_stream(sample_sbas_eph, 0, 561600.0, 6)
    blks = D.decode_messages(stream)
    rec = D.reconstruct_ephemeris(blks, prn=120)

    assert rec["system"] == "S"
    assert rec["prn"] == 120
    assert abs(rec["x_m"] - sample_sbas_eph["x_m"]) < 0.1
    assert abs(rec["y_m"] - sample_sbas_eph["y_m"]) < 0.1
    assert abs(rec["z_m"] - sample_sbas_eph["z_m"]) < 0.1
    assert abs(rec["vx"] - sample_sbas_eph["vx"]) < 1e-4
    assert abs(rec["vy"] - sample_sbas_eph["vy"]) < 1e-4
    assert abs(rec["vz"] - sample_sbas_eph["vz"]) < 1e-4
    assert abs(rec["ax"] - sample_sbas_eph["ax"]) < 1e-6
    assert abs(rec["ay"] - sample_sbas_eph["ay"]) < 1e-6
    assert abs(rec["az"] - sample_sbas_eph["az"]) < 1e-6


def test_reconstruct_ephemeris_missing_type9_raises():
    null_blocks = [{"type": 63, "crc_ok": True, "data": [0] * 212}]
    with pytest.raises(ValueError, match="need at least one CRC-valid SBAS Type 9 block"):
        D.reconstruct_ephemeris(null_blocks, prn=120)


def test_demod_symbols_sbas_l1(sample_sbas_eph):
    prn = 120
    fs = 4_092_000.0
    n_ms = 40  # 40 ms = 20 symbols
    n_samples = int(fs * (n_ms * 0.001))
    t = np.arange(n_samples) / fs

    syms, _ = S.nav_stream(sample_sbas_eph, 0, 561600.0, 1)
    test_syms = syms[:20]

    spms = int(round(fs * 0.001))
    sym_samples = np.repeat(test_syms, 2 * spms)[:n_samples]

    c_raw = _lib.code(2, prn, 1023, 0)[0]
    chip_idx = np.floor(t * 1.023e6).astype(int) % 1023
    code = c_raw[chip_idx].astype(np.float32)

    fd = 1200.0
    carr = np.exp(2j * np.pi * fd * t).astype(np.complex64)
    iq = (code * sym_samples).astype(np.complex64) * carr

    demod = D.demod_symbols_sbas_l1(iq, fs, prn, dopp_hz=fd, code_phase_chips=0.0)
    assert len(demod) == len(test_syms)

    exp01 = np.array([1 if s > 0 else 0 for s in test_syms])
    match_norm = np.mean(demod == exp01)
    match_inv = np.mean(demod == (1 - exp01))
    assert match_norm > 0.95 or match_inv > 0.95
