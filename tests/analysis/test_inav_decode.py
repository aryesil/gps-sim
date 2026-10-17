"""Unit tests for Galileo E1 I/NAV decoder (backend.analysis.inav_decode).

Follows TDD principle: tests written before decoder implementation.
"""
import math
import numpy as np
import pytest

from backend.analysis import inav_encode as I
from backend.ephem import ephemeris

_WEEK, _SOW = 2325, 561600.0


def _inav_af0(erec):
    """I/NAV af0 is E1-E5b referenced. The fixture record is F/NAV (E5a)
    referenced, so the encoder shifts af0 by BGD(E1,E5a)-BGD(E1,E5b)."""
    from backend import geometry
    return erec["af0"] + geometry.galileo_clock_shift_s(erec, "E5B")


@pytest.fixture(scope="module")
def erec():
    e = ephemeris.parse_rinex_multi("tests/fixtures/brdc_mixed.rnx", ("E",),
                                    require=())
    rec = ephemeris.align_epochs(e, _WEEK, _SOW)
    rec = [v for k, v in rec.items() if k[0] == "E"][0]
    rec["prn"] = 5
    return rec


def test_deinterleave_and_fec():
    from backend.analysis import inav_decode as D

    rng = np.random.default_rng(42)
    data = rng.integers(0, 2, 114).tolist() + [0] * 6
    coded = I.conv_encode(data)
    interleaved = I.interleave_30x8(coded)

    deinterleaved = D.deinterleave_30x8(interleaved)
    assert deinterleaved == coded

    decoded = D.viterbi_free_decode(deinterleaved)
    assert decoded == data


def test_decode_page_parts(erec):
    from backend.analysis import inav_decode as D

    page = I.build_page(1, erec, int(_SOW), 1325)
    even = page[:250]
    odd = page[250:]

    res = D.decode_page(even, odd)
    assert res is not None
    assert res["crc_ok"] is True
    assert res["type"] == 1
    assert len(res["word_bits"]) == 128


def test_viterbi_corrects_single_error(erec):
    from backend.analysis import inav_decode as D

    page = I.build_page(1, erec, int(_SOW), 1325)
    even = page[:250]
    odd = list(page[250:])
    # Tek sembol hatası (tek yarıda) Viterbi fallback ile düzeltilir
    odd[100] ^= 1

    res = D.decode_page(even, odd)
    assert res is not None
    assert res["crc_ok"] is True


def test_viterbi_corrects_single_error_in_even_part(erec):
    """Çift sayfadaki tek sembol hatasının Viterbi fallback ile kurtarılması."""
    from backend.analysis import inav_decode as D

    page = I.build_page(1, erec, int(_SOW), 1325)
    even = list(page[:250])
    odd = page[250:]
    even[80] ^= 1

    res = D.decode_page(even, odd)
    assert res is not None
    assert res["crc_ok"] is True
    assert res["type"] == 1


def test_viterbi_corrects_two_errors_across_fec_blocks(erec):
    """Farklı FEC bloklarında (biri çift, biri tek sayfada) 1'er sembol hatasının kurtarılması."""
    from backend.analysis import inav_decode as D

    page = I.build_page(1, erec, int(_SOW), 1325)
    even = list(page[:250])
    odd = list(page[250:])
    even[60] ^= 1
    odd[120] ^= 1

    res = D.decode_page(even, odd)
    assert res is not None
    assert res["crc_ok"] is True
    assert res["type"] == 1


def test_decode_page_crc_corrupted(erec):
    from backend.analysis import inav_decode as D

    page = I.build_page(1, erec, int(_SOW), 1325)
    even = page[:250]
    odd = list(page[250:])
    # 30x8 serpiştiricinin dağıtma kapasitesini ve K=7 Viterbi düzeltme sınırını
    # aşacak büyüklükte (50 sembol) bir hata bloğu enjekte et
    for idx in range(50, 100):
        odd[idx] ^= 1

    res = D.decode_page(even, odd)
    assert res is None or res.get("crc_ok") is False


def test_decode_word_types(erec):
    from backend.analysis import inav_decode as D

    for wtype in (1, 2, 3, 4, 5, 0):
        page = I.build_page(wtype, erec, int(_SOW), 1325)
        res = D.decode_page(page[:250], page[250:])
        assert res is not None
        assert res["type"] == wtype
        assert res["crc_ok"] is True
        fields = D.decode_word_fields(wtype, res["word_bits"])
        assert isinstance(fields, dict)
        if wtype == 1:
            assert fields["toe"] == pytest.approx(round(erec["toe"] / 60.0) * 60.0, abs=60.0)
            assert fields["m0"] == pytest.approx(erec["m0"] / math.pi, abs=1e-8)
            assert fields["e"] == pytest.approx(erec["e"], abs=1e-8)
            assert fields["sqrtA"] == pytest.approx(erec["sqrtA"], abs=1e-3)
        elif wtype == 4:
            assert fields["prn"] == erec["prn"]
            assert fields["af0"] == pytest.approx(_inav_af0(erec), abs=1e-9)


def test_decode_messages_from_stream(erec):
    from backend.analysis import inav_decode as D

    # 12 seconds stream -> 6 pages: types [1, 2, 3, 4, 5, 0]
    stream = I.nav_stream(erec, 1325, _SOW, 12.0)
    # Convert {-1, +1} to {0, 1} where +1 -> 1, -1 -> 0
    sym01 = [1 if s > 0 else 0 for s in stream]

    msgs = D.decode_messages(sym01)
    assert len(msgs) >= 6
    types = [m["type"] for m in msgs[:6]]
    assert types == [1, 2, 3, 4, 5, 0]
    assert all(m["crc_ok"] for m in msgs[:6])


def test_decode_messages_with_offset_and_polarity_flip(erec):
    from backend.analysis import inav_decode as D

    stream = I.nav_stream(erec, 1325, _SOW, 14.0)
    sym01 = [1 if s > 0 else 0 for s in stream]

    # Add 17 arbitrary leading symbols and invert polarity (0 <-> 1)
    shifted = [0] * 17 + [s ^ 1 for s in sym01]

    msgs = D.decode_messages(shifted)
    assert len(msgs) >= 6
    types = [m["type"] for m in msgs[:6]]
    assert types == [1, 2, 3, 4, 5, 0]
    assert all(m["crc_ok"] for m in msgs[:6])


def test_reconstruct_ephemeris(erec):
    from backend.analysis import inav_decode as D

    stream = I.nav_stream(erec, 1325, _SOW, 12.0)
    sym01 = [1 if s > 0 else 0 for s in stream]
    msgs = D.decode_messages(sym01)

    rec = D.reconstruct_ephemeris(msgs)
    assert rec["system"] == "E"
    assert rec["prn"] == 5
    assert rec["sqrtA"] == pytest.approx(erec["sqrtA"], abs=1e-3)
    assert rec["e"] == pytest.approx(erec["e"], abs=1e-8)
    assert rec["m0"] == pytest.approx(erec["m0"], abs=1e-7)
    assert rec["omega0"] == pytest.approx(erec["omega0"], abs=1e-7)
    assert rec["i0"] == pytest.approx(erec["i0"], abs=1e-7)
    assert rec["omega"] == pytest.approx(erec["omega"], abs=1e-7)
    assert rec["omega_dot"] == pytest.approx(erec["omega_dot"], abs=1e-10)
    assert rec["idot"] == pytest.approx(erec["idot"], abs=1e-10)
    assert rec["delta_n"] == pytest.approx(erec["delta_n"], abs=1e-10)
    assert rec["cuc"] == pytest.approx(erec["cuc"], abs=1e-7)
    assert rec["cus"] == pytest.approx(erec["cus"], abs=1e-7)
    assert rec["crc"] == pytest.approx(erec["crc"], abs=1.0)
    assert rec["crs"] == pytest.approx(erec["crs"], abs=1.0)
    assert rec["cic"] == pytest.approx(erec["cic"], abs=1e-7)
    assert rec["cis"] == pytest.approx(erec["cis"], abs=1e-7)
    assert rec["af0"] == pytest.approx(_inav_af0(erec), abs=1e-9)
    assert rec["af1"] == pytest.approx(erec["af1"], abs=1e-12)
    assert rec["af2"] == pytest.approx(erec["af2"], abs=1e-15)
    assert rec["toe"] == pytest.approx(erec["toe"], abs=60.0)
    assert rec["toc"] == pytest.approx(erec["toc"], abs=60.0)
    assert rec["tgd"] == pytest.approx(erec.get("tgd", 0.0), abs=1e-9)
    assert rec["gal_week"] == 1325


def test_reconstruct_ephemeris_insufficient_words():
    from backend.analysis import inav_decode as D

    # Missing word 4
    msgs = [
        {"type": 1, "crc_ok": True, "fields": {}},
        {"type": 2, "crc_ok": True, "fields": {}},
        {"type": 3, "crc_ok": True, "fields": {}},
    ]
    with pytest.raises(ValueError, match="need CRC-valid I/NAV word types 1, 2, 3 and 4"):
        D.reconstruct_ephemeris(msgs)


def test_demod_symbols_e1b():
    from backend.analysis import inav_decode as D
    from backend.synth import _lib

    prn = 5
    code = _lib.code(5, prn, 4092, 0)[0].astype(np.float64)
    fs = 4_092_000.0
    sym_s = 0.004
    npp = int(round(fs * sym_s))
    nsym = 250

    rng = np.random.default_rng(999)
    test_bits = rng.integers(0, 2, nsym)
    test_syms = np.where(test_bits > 0, 1.0, -1.0)

    dopp = 1500.0
    code_dopp = 1.023e6 / 1575.42e6 * dopp
    total_samples = nsym * npp
    iq = _lib.debug_mix_range_ex(code.astype(np.int8), 1.023e6, 0.0, code_dopp, dopp, fs, 0, total_samples, sys=5, sub_hz=1.023e6)
    for k in range(nsym):
        iq[k * npp:(k + 1) * npp] *= test_syms[k]

    demod = D.demod_symbols_e1b(iq, fs, prn, dopp_hz=dopp, code_phase_chips=0.0)
    assert len(demod) == nsym
    match_direct = np.array_equal(demod, test_bits)
    match_inverted = np.array_equal(demod, 1 - test_bits)
    assert match_direct or match_inverted

