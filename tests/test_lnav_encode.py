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
