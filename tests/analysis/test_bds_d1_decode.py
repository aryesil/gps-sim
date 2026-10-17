"""Unit tests for BeiDou B1I D1 decoder (backend.analysis.bds_d1_decode)."""
import math
import numpy as np
import pytest

from backend.analysis import bds_d1_decode as D
from backend.analysis import bds_d1_encode as B
from backend.ephem import ephemeris

_WEEK, _SOW = 910, 561600.0
_IONO = ((1.1e-8, 1.5e-8, -6.0e-8, -1.2e-7), (1.0e5, 1.3e5, -6.5e4, -4.0e5))
_SC = math.pi


@pytest.fixture(scope="module")
def crec():
    e = ephemeris.parse_rinex_multi("tests/fixtures/brdc_mixed.rnx", ("C",), require=())
    rec = ephemeris.align_epochs(e, _WEEK, _SOW)
    rec = dict([v for k, v in rec.items() if k[0] == "C"][0])
    rec["prn"] = 6
    rec["toe"] = rec["toc"] = 561600.0 - 14.0 - (561600.0 - 14.0) % 8
    return rec


def test_bch_decode_clean():
    rng = np.random.default_rng(101)
    for _ in range(10):
        data11 = rng.integers(0, 2, 11).tolist()
        cw = B.bch_encode(data11)
        assert D.bch_ok(cw) is True
        dec = D.bch_decode(cw)
        assert dec == data11


def test_bch_decode_single_bit_error_correction():
    rng = np.random.default_rng(202)
    data11 = rng.integers(0, 2, 11).tolist()
    cw = B.bch_encode(data11)
    for bit_idx in range(15):
        corrupted = list(cw)
        corrupted[bit_idx] ^= 1
        assert D.bch_ok(corrupted) is False
        dec = D.bch_decode(corrupted, correct=True)
        assert dec == data11, f"Failed correcting bit {bit_idx}"


def test_deinterleave_words():
    a = list(range(15))
    b = list(range(100, 115))
    interleaved = B.interleave2(a, b)
    dec_a, dec_b = D.deinterleave_words(interleaved)
    assert dec_a == a and dec_b == b

    # Test multi-word block (270 bits = 9 words)
    multi_words = interleaved * 9
    res = D.deinterleave_words(multi_words)
    assert isinstance(res, list)
    assert len(res) == 9
    for pair in res:
        assert pair == (a, b)


def test_decode_subframe(crec):
    sow = 561594
    sf = B.d1_subframe(2, crec, sow, _WEEK, _IONO)
    dec = D.decode_subframe(sf)

    assert dec["bch_ok"] is True
    assert dec["fra_id"] == 2
    assert dec["subframe_id"] == 2
    assert dec["sow"] == sow
    assert len(dec["info"]) == 224


def test_decode_messages_from_stream(crec):
    sow = 561594
    stream, rate = B.nav_stream(crec, _WEEK, sow, 24.0)
    assert rate == 50.0

    msgs = D.decode_messages(stream)
    assert len(msgs) >= 3
    fra_ids = [m["fra_id"] for m in msgs]
    assert {1, 2, 3}.issubset(set(fra_ids))
    assert all(m["bch_ok"] for m in msgs)


def test_decode_messages_with_offset_and_polarity_flip(crec):
    sow = 561594
    stream, _ = B.nav_stream(crec, _WEEK, sow, 24.0)
    bits = [1 if s > 0 else 0 for s in stream]

    # Add 13-bit arbitrary lead and 180-deg polarity flip
    shifted = [0, 1, 0, 1, 1, 0, 0, 1, 1, 1, 0, 0, 1] + [b ^ 1 for b in bits]
    msgs = D.decode_messages(shifted)

    assert len(msgs) >= 3
    fra_ids = [m["fra_id"] for m in msgs]
    assert {1, 2, 3}.issubset(set(fra_ids))
    assert all(m["bch_ok"] for m in msgs)


def test_reconstruct_ephemeris(crec):
    sow = 561594
    sf = [B.d1_subframe(k, crec, sow + 6 * (k - 1), _WEEK, _IONO) for k in (1, 2, 3)]
    subframes = [D.decode_subframe(s) for s in sf]

    rec = D.reconstruct_ephemeris(subframes)
    assert rec["system"] == "C"
    assert rec["bdt_week"] == _WEEK
    assert rec["toe"] == crec["toe"]
    assert rec["toc"] == crec["toc"]

    assert rec["af0"] == pytest.approx(crec["af0"], abs=2**-33)
    assert rec["af1"] == pytest.approx(crec["af1"], abs=2**-50)
    assert rec["af2"] == pytest.approx(crec["af2"], abs=2**-66)
    assert rec["tgd"] == pytest.approx(crec["tgd"], abs=1e-10)
    assert rec["tgd2"] == pytest.approx(crec["tgd2"], abs=1e-10)

    assert rec["sqrtA"] == pytest.approx(crec["sqrtA"], abs=2**-19)
    assert rec["A"] == pytest.approx(crec["sqrtA"] ** 2, rel=1e-6)
    assert rec["e"] == pytest.approx(crec["e"], abs=2**-33)
    assert rec["m0"] == pytest.approx(crec["m0"], abs=1e-8)
    assert rec["delta_n"] == pytest.approx(crec["delta_n"], rel=1e-4)
    assert rec["i0"] == pytest.approx(crec["i0"], abs=1e-8)
    assert rec["idot"] == pytest.approx(crec["idot"], abs=1e-12)
    assert rec["omega0"] == pytest.approx(crec["omega0"], abs=1e-8)
    assert rec["omega"] == pytest.approx(crec["omega"], abs=1e-8)
    assert rec["omega_dot"] == pytest.approx(crec["omega_dot"], rel=1e-4)

    assert rec["cuc"] == pytest.approx(crec["cuc"], abs=2**-31)
    assert rec["cus"] == pytest.approx(crec["cus"], abs=2**-31)
    assert rec["crc"] == pytest.approx(crec["crc"], abs=2**-6)
    assert rec["crs"] == pytest.approx(crec["crs"], abs=2**-6)
    assert rec["cic"] == pytest.approx(crec["cic"], abs=2**-31)
    assert rec["cis"] == pytest.approx(crec["cis"], abs=2**-31)

    assert rec["iono_alpha"][0] == pytest.approx(_IONO[0][0], abs=2**-30)
    assert rec["iono_beta"][0] == pytest.approx(_IONO[1][0], abs=2**11)


def test_reconstruct_ephemeris_insufficient_subframes():
    # Only SF 1 and 2
    subframes = [
        {"fra_id": 1, "info": [0] * 224},
        {"fra_id": 2, "info": [0] * 224},
    ]
    with pytest.raises(ValueError, match="need subframes 1, 2 and 3"):
        D.reconstruct_ephemeris(subframes)


def test_demod_symbols_b1i():
    from backend.synth import _lib

    prn = 6
    code, sec = _lib.code(3, prn, 2046, 20)
    fs = 4_092_000.0
    dopp = 1500.0
    code_dopp = 2.046e6 / 1561.098e6 * dopp
    n_ms = int(round(fs * 0.001))
    n_sym = n_ms * 20
    nsym = 25

    rng = np.random.default_rng(999)
    test_bits = rng.integers(0, 2, nsym)
    test_syms = np.where(test_bits > 0, 1.0, -1.0)

    total_samples = nsym * n_sym
    iq = _lib.debug_mix_range_ex(
        code,
        2.046e6,
        0.0,
        code_dopp,
        dopp,
        fs,
        0,
        total_samples,
        sys=3,
        sec=sec,
        sec_len=20,
        sec_rate=1000.0,
    )
    for k in range(nsym):
        iq[k * n_sym : (k + 1) * n_sym] *= test_syms[k]

    demod = D.demod_symbols_b1i(
        iq, fs, prn, dopp_hz=dopp, code_phase_chips=0.0
    )
    assert len(demod) >= nsym - 1
    # Compare common length prefix
    common_len = min(len(demod), len(test_bits))
    match_direct = np.array_equal(demod[:common_len], test_bits[:common_len])
    match_inverted = np.array_equal(demod[:common_len], 1 - test_bits[:common_len])
    assert match_direct or match_inverted
