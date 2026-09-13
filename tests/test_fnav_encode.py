"""Galileo E5a-I F/NAV encoder -- structure, FEC, interleaver, CRC, and a
clean-channel field round-trip."""
import numpy as np
import pytest

from backend.analysis import fnav_encode as F
from backend.ephem import ephemeris

_WEEK, _SOW = 2434, 194440.0


@pytest.fixture(scope="module")
def erec():
    e = ephemeris.parse_rinex_multi("tests/fixtures/brdc_mixed.rnx", ("E",),
                                    require=())
    rec = ephemeris.align_epochs(e, _WEEK, _SOW)
    rec = [v for k, v in rec.items() if k[0] == "E"][0]
    rec["prn"] = 2
    return rec


def test_conv_encode_reference_vectors():
    # Same G1/G2 as inav_encode -- identical to its own reference vectors.
    assert F.conv_encode([1]) == [1, 0]
    assert F.conv_encode([1, 0]) == [1, 0, 0, 0]
    assert F.conv_encode([0, 0, 0]) == [0, 1, 0, 1, 0, 1]     # G2 inverted


def test_conv_encode_is_invertible_on_a_clean_stream():
    rng = np.random.default_rng(0)
    data = rng.integers(0, 2, 244).tolist()
    assert F._viterbi_free_decode(F.conv_encode(data)) == data


def test_interleaver_permutation():
    il = F.interleave_8x61(list(range(488)))
    assert il[:5] == [0, 61, 122, 183, 244]
    assert F.deinterleave_8x61(il) == list(range(488))


def test_page_layout_and_crc(erec):
    page = F.build_page(1, erec, erec["prn"], int(_SOW), 1325)
    assert len(page) == 500
    assert page[:12] == F._PREAMBLE
    assert F.check_page_crc(page)


def test_word_fields_round_trip_clean(erec):
    page = F.build_page(2, erec, erec["prn"], int(_SOW), 1325)
    wt, message = F.decode_word(page)
    assert wt == 2
    # word 2 layout: type(6) iodnav(10) m0(32) omega_dot(24) e(32) sqrtA(32) ...
    off = 6 + 10
    m0 = _s(message[off:off + 32]) * (2 ** -31) * np.pi
    off += 32 + 24
    e = _u(message[off:off + 32]) * (2 ** -33)
    sqrta = _u(message[off + 32:off + 64]) * (2 ** -19)
    assert m0 == pytest.approx(erec["m0"], abs=1e-8)
    assert e == pytest.approx(erec["e"], abs=1e-9)
    assert sqrta == pytest.approx(erec["sqrtA"], abs=1e-3)


def test_nav_stream_rate_and_alphabet(erec):
    arr, rate = F.nav_stream(erec, erec["prn"], 1325, _SOW, 6)
    assert rate == 50.0
    assert arr.dtype == np.int8
    assert sorted(set(arr.tolist())) == [-1, 1]
    assert arr.size % 500 == 0
    arr2, _ = F.nav_stream(erec, erec["prn"], 1325, _SOW, 6)
    assert np.array_equal(arr, arr2)


def _s(bits):
    v = int("".join(map(str, bits)), 2)
    n = len(bits)
    return v - (1 << n) if v >= (1 << (n - 1)) else v


def _u(bits):
    return int("".join(map(str, bits)), 2)
