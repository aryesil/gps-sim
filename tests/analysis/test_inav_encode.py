"""SP-D: Galileo E1-B I/NAV encoder -- structure, FEC, interleaver, CRC,
and a clean-channel field round-trip."""
import numpy as np
import pytest

from backend.analysis import inav_encode as I
from backend.ephem import ephemeris

_WEEK, _SOW = 2325, 561600.0


@pytest.fixture(scope="module")
def erec():
    e = ephemeris.parse_rinex_multi("tests/fixtures/brdc_mixed.rnx", ("E",),
                                    require=())
    rec = ephemeris.align_epochs(e, _WEEK, _SOW)
    rec = [v for k, v in rec.items() if k[0] == "E"][0]
    rec["prn"] = 5
    return rec


def test_conv_encode_reference_vectors():
    assert I.conv_encode([1]) == [1, 0]
    # G1=0o117, G2=0o155 (G2 tersli): [1, 0] girişi için t=0 -> [1, 0], t=1 -> [1, 1]
    assert I.conv_encode([1, 0]) == [1, 0, 1, 1]
    assert I.conv_encode([0, 0, 0]) == [0, 1, 0, 1, 0, 1]     # G2 inverted


def test_conv_encode_impulse_response():
    """Giriş [1,0,0,0,0,0,0] için Galileo OS SIS ICD Section 4.1.3 impulse yanıtı."""
    expected = [1, 0, 1, 1, 1, 0, 1, 0, 0, 1, 0, 0, 1, 0]
    assert I.conv_encode([1, 0, 0, 0, 0, 0, 0]) == expected


def test_conv_encode_is_invertible_on_a_clean_stream():
    rng = np.random.default_rng(0)
    data = rng.integers(0, 2, 240).tolist()
    assert I._viterbi_free_decode(I.conv_encode(data)) == data


def test_interleaver_permutation():
    il = I.interleave_30x8(list(range(240)))
    # ICD 4.1.4: 8 satır x 30 sütun, sütun sütun yaz / satır satır oku
    # -> satır 0 = c[0], c[8], c[16], ... (bağımsız GNSS-SDR deinterleaver formu)
    assert il[:5] == [0, 8, 16, 24, 32]
    assert il[30:32] == [1, 9]
    assert I.deinterleave_30x8(il) == list(range(240))


def test_interleaver_matches_receiver_form():
    """Alıcı formu out[c*8+r] = in[r*30+c] verici çıkışını geri çözmeli.
    Encoder/decoder simetrik yanlışını yakalayan bağımsız kontrol."""
    c = list(range(240))
    il = I.interleave_30x8(c)
    out = [0] * 240
    for r in range(8):
        for col in range(30):
            out[col * 8 + r] = il[r * 30 + col]
    assert out == c


def test_page_layout_and_crc(erec):
    page = I.build_page(1, erec, int(_SOW), 1325)
    assert len(page) == 500
    assert page[:10] == I.SYNC_PATTERN
    assert page[250:260] == I.SYNC_PATTERN
    assert I.check_page_crc(page)


def test_word_fields_round_trip_clean(erec):
    page = I.build_page(1, erec, int(_SOW), 1325)
    wt, word = I.decode_word(page)
    assert wt == 1
    # word 1 layout: type(6) iodnav(10) toe(14) M0(32) e(32) sqrtA(32) spare(2)
    off = 6 + 10 + 14
    m0 = _s(word[off:off + 32]) * (2 ** -31) * np.pi
    e = _u(word[off + 32:off + 64]) * (2 ** -33)
    sqrta = _u(word[off + 64:off + 96]) * (2 ** -19)
    assert m0 == pytest.approx(erec["m0"], abs=1e-8)
    assert e == pytest.approx(erec["e"], abs=1e-9)
    assert sqrta == pytest.approx(erec["sqrtA"], abs=1e-3)


def test_nav_stream_rate_and_alphabet(erec):
    arr = I.nav_stream(erec, 1325, _SOW, 6)
    assert arr.dtype == np.int8
    assert sorted(set(arr.tolist())) == [-1, 1]
    assert arr.size % 500 == 0
    assert np.array_equal(arr, I.nav_stream(erec, 1325, _SOW, 6))


def _s(bits):
    v = int("".join(map(str, bits)), 2)
    n = len(bits)
    return v - (1 << n) if v >= (1 << (n - 1)) else v


def _u(bits):
    return int("".join(map(str, bits)), 2)
