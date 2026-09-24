import math

import numpy as np
import pytest

from backend.analysis import lnav_encode as le


# --- Task 1: parity + word framing ---------------------------------------

def test_bits_of_big_endian():
    assert le.bits_of(0b101, 4) == [0, 1, 0, 1]


def test_twos_roundtrip_negative():
    val, scale, n = -5.0e-4, 2 ** -31, 22
    raw = le.twos(val, scale, n)
    signed = raw - (1 << n) if raw >= 1 << (n - 1) else raw
    assert signed == int(round(val / scale))
    assert 0 <= raw < (1 << n)


def test_parity_all_zero_source_is_all_zero():
    assert le.parity_bits([0] * 24, 0, 0) == [0, 0, 0, 0, 0, 0]


def test_word_all_zero_source_prev_zero_is_all_zero():
    assert le.make_word([0] * 24, 0, 0) == [0] * 30


def test_word_d30prev_one_inverts_data_and_sets_parity():
    # source all zero, D29*=0, D30*=1:
    #   data bits invert to all ones; parity folds only D29*/D30*:
    #   D25=D29*=0, D26=D30*=1, D27=D29*=0, D28=D30*=1, D29=D30*=1, D30=D29*=0
    w = le.make_word([0] * 24, 0, 1)
    assert w[:24] == [1] * 24
    assert w[24:] == [0, 1, 0, 1, 1, 0]


def test_parity_nontrivial_hand_vector():
    # d1=1, rest 0, D29*=D30*=0. d1 appears in eq 25, 27, 29 only.
    assert le.parity_bits([1] + [0] * 23, 0, 0) == [1, 0, 1, 0, 1, 0]


def test_how_solve_bits_make_trailing_parity_zero():
    src = le.HOW_solve_bits(tow_count=12345, subframe_id=1, D29_prev=1, D30_prev=0)
    w = le.make_word(src, 1, 0)
    assert w[28] == 0 and w[29] == 0


@pytest.mark.parametrize("d29,d30", [(0, 0), (0, 1), (1, 0), (1, 1)])
def test_parity_is_self_consistent_for_any_prev(d29, d30):
    src = [(i * 7 + 3) % 2 for i in range(24)]
    w = le.make_word(src, d29, d30)
    # recovering source from transmitted bits and re-deriving parity matches
    rec = [b ^ d30 for b in w[:24]]
    assert w[24:] == le.parity_bits(rec, d29, d30)


# --- Task 2: TLM / HOW / subframe skeleton ------------------------------

def test_tlm_starts_with_preamble():
    assert le.tlm_word(0, 0)[:8] == le.PREAMBLE


def test_subframe_is_300_bits_and_starts_with_preamble():
    sf = le.subframe([[0] * 24] * 8, tow_count=100, subframe_id=1)
    assert len(sf) == 300
    assert sf[:8] == le.PREAMBLE


def test_subframe_word_boundaries_parity_check():
    sf = le.subframe([[1, 0] * 12] * 8, tow_count=7, subframe_id=3)
    d29 = d30 = 0
    for i in range(10):
        w = sf[i * 30:(i + 1) * 30]
        src = [b ^ d30 for b in w[:24]]
        assert w[24:] == le.parity_bits(src, d29, d30)
        d29, d30 = w[28], w[29]


def test_subframe_rejects_wrong_word_count():
    with pytest.raises(ValueError):
        le.subframe([[0] * 24] * 7, tow_count=1, subframe_id=1)


# --- Task 3: subframe 1 ------------------------------------------------

_EPH = {
    "af0": -1.23e-4, "af1": 4.5e-12, "af2": 0.0, "toc": 100800.0,
    "tgd": -5.1e-9, "iodc": 42, "health": 0,
    "iode": 42, "crs": -12.3, "delta_n": 4.9e-9, "m0": 0.75,
    "cuc": -6.1e-7, "e": 0.012, "cus": 7.2e-6, "sqrtA": 5153.6,
    "toe": 100800.0, "cic": 1.1e-8, "omega0": -1.9, "cis": -9.0e-8,
    "i0": 0.96, "crc": 210.0, "omega": 0.55, "omega_dot": -8.1e-9,
    "idot": 1.3e-10,
}


def _source_words(sf):
    """Recover the 10 words' 24 source data bits from a 300-bit subframe."""
    d29 = d30 = 0
    words = []
    for i in range(10):
        w = sf[i * 30:(i + 1) * 30]
        words.append([b ^ d30 for b in w[:24]])
        d29, d30 = w[28], w[29]
    return words


def test_subframe1_length_and_preamble():
    sf = le.subframe1(_EPH, week=200, tow_count=100)
    assert len(sf) == 300 and sf[:8] == le.PREAMBLE


def test_subframe1_week_number_field():
    w = _source_words(le.subframe1(_EPH, week=200, tow_count=100))
    assert int("".join(map(str, w[2][0:10])), 2) == 200


def test_subframe1_toc_and_af0_scales():
    w = _source_words(le.subframe1(_EPH, week=200, tow_count=100))
    toc_raw = int("".join(map(str, w[7][8:24])), 2)
    assert toc_raw == round(_EPH["toc"] / 16)
    af0_raw = int("".join(map(str, w[9][0:22])), 2)
    assert af0_raw == le.twos(_EPH["af0"], 2 ** -31, 22)


# --- Task 4: subframes 2 & 3 -----------------------------------------

def _u(bits):
    v = 0
    for b in bits:
        v = (v << 1) | b
    return v


def test_subframe2_iode_and_sqrta():
    w = _source_words(le.subframe2(_EPH, tow_count=100))
    assert _u(w[2][0:8]) == _EPH["iode"]
    sqrta_raw = _u(w[7][16:24] + w[8][0:24])
    assert sqrta_raw == round(_EPH["sqrtA"] / 2 ** -19)


def test_subframe2_m0_semicircle_conversion():
    w = _source_words(le.subframe2(_EPH, tow_count=100))
    raw = _u(w[3][16:24] + w[4][0:24])
    assert raw == le.twos(_EPH["m0"] / math.pi, 2 ** -31, 32)


def test_subframe3_omega_dot_24bit():
    w = _source_words(le.subframe3(_EPH, tow_count=100))
    raw = _u(w[8][0:24])
    assert raw == le.twos(_EPH["omega_dot"] / math.pi, 2 ** -43, 24)


def test_subframe3_i0_split():
    w = _source_words(le.subframe3(_EPH, tow_count=100))
    raw = _u(w[4][16:24] + w[5][0:24])
    assert raw == le.twos(_EPH["i0"] / math.pi, 2 ** -31, 32)


# --- Task 5: subframes 4 & 5 ----------------------------------------

_HDR = {"iono_alpha": [1.1e-8, 0.0, -5.96e-8, 0.0],
        "iono_beta": [88064.0, 0.0, -196608.0, 0.0],
        "utc": {"A0": 1.86e-9, "A1": 3.5e-15, "tot": 319488, "WNt": 200,
                "dtLS": 18, "WNlsf": 201, "DN": 7, "dtLSF": 18}}


def test_sf4_page18_length_and_preamble():
    sf = le.subframe4(18, {}, _HDR, tow_count=1)
    assert len(sf) == 300 and sf[:8] == le.PREAMBLE


def test_sf4_page18_alpha0_scale():
    w = _source_words(le.subframe4(18, {}, _HDR, tow_count=1))
    a0 = _u(w[2][8:16])
    assert a0 == le.twos(_HDR["iono_alpha"][0], 2 ** -30, 8)


def test_sf5_page_almanac_present_and_zeroed():
    assert len(le.subframe5(3, {3: _EPH}, tow_count=1)) == 300
    assert len(le.subframe5(3, {}, tow_count=1)) == 300


def test_all_25_pages_assemble_for_both_subframes():
    for p in range(1, 26):
        assert len(le.subframe4(p, {}, None, 1)) == 300
        assert len(le.subframe5(p, {}, 1)) == 300


# --- Task 6: frame_bits + nav_stream --------------------------------

def test_frame_bits_is_1500_and_five_preambles():
    f = le.frame_bits(_EPH, _HDR, week=200, tow_count=100, sf45_page=1)
    assert len(f) == 1500
    for k in range(5):
        assert f[k * 300:k * 300 + 8] == le.PREAMBLE


def test_nav_stream_length_and_values():
    s = le.nav_stream(_EPH, _HDR, week=200, tow0_sow=100801.7, duration_s=10)
    assert s.dtype == np.int8
    assert set(np.unique(s)).issubset({-1, 1})
    assert len(s) == 50 * (10 + 30)


def test_nav_stream_starts_on_6s_grid_with_preamble():
    s = le.nav_stream(_EPH, _HDR, week=200, tow0_sow=100801.0, duration_s=6)
    assert list(s[:8]) == [1 if b == 0 else -1 for b in le.PREAMBLE]


def test_nav_stream_is_deterministic():
    a = le.nav_stream(_EPH, _HDR, 200, 100801.0, 6)
    b = le.nav_stream(_EPH, _HDR, 200, 100801.0, 6)
    assert np.array_equal(a, b)


def _sf_ids(stream):
    from backend.analysis import lnav_decode
    return [(h["subframe_id"], h["tow_count"])
            for h in lnav_decode.find_frame((stream < 0).astype(np.int8))]


def test_nav_stream_subframe_id_follows_gps_time():
    # A frame starts every 30 s of the week: the subframe sent at TOW-count
    # n (HOW carries n+1) is ((n % 5) + 1), whatever time the stream starts.
    s = le.nav_stream(_EPH, _HDR, 200, 100813.0, 30)
    ids = _sf_ids(s)
    assert ids
    for sf_id, how_tow in ids:
        assert sf_id == ((how_tow - 1) % 5) + 1


def test_nav_stream_is_time_invariant():
    # Live mode restarts the stream every segment: a stream started later
    # must be the same bits as the earlier stream, shifted.
    a = le.nav_stream(_EPH, _HDR, 200, 100801.0, 60)
    for d in (6, 12, 30, 36):
        b = le.nav_stream(_EPH, _HDR, 200, 100801.0 + d, 20)
        off = (int((100801.0 + d) // 6) - int(100801.0 // 6)) * 6 * le.NAV_BIT_HZ
        assert np.array_equal(a[off:off + b.size], b)
