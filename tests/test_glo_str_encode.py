"""SP-D: GLONASS L1OF string encoder -- meander, Hamming, field round-trip."""
import numpy as np
import pytest

from backend.analysis import glo_str_encode as G
from backend.ephem import ephemeris


@pytest.fixture(scope="module")
def rrec():
    e = ephemeris.parse_rinex_multi("tests/fixtures/brdc_mixed.rnx", ("R",),
                                    require=())
    rec = [v for k, v in e.items() if k[0] == "R"][0]
    rec["prn"] = 1
    return rec


def test_meander_doubles_each_bit_with_opposite_halves():
    a, rate = G.nav_stream({"x_m": 0, "y_m": 0, "z_m": 0}, 0, 0.0, 2)
    assert rate == 100.0
    # every symbol pair is +1,-1 or -1,+1 -> sums to 0
    pairs = a.reshape(-1, 2).sum(axis=1)
    assert np.all(pairs == 0)


def test_hamming_zero_string_and_single_bit_error_detected():
    s = G.hamming_string([0] * 76)
    assert len(s) == 85 and G.hamming_check(s)
    bad = list(s)
    bad[20] ^= 1
    assert not G.hamming_check(bad)


def test_string_fields_round_trip_clean(rrec):
    arr, _r = G.nav_stream(rrec, 0, 561600.0, 6)
    frame = G.decode_frame(arr)
    assert {1, 2, 3, 4} <= set(frame)
    # string 1 data: [tk12][vx s-m 24][ax s-m 5][x_m s-m 27]  (the first 4
    # bits, tk's MSBs, are overwritten with the string index by build_string).
    d1 = frame[1]
    off = 12 + 24 + 5
    x = G._from_sign_mag(d1[off:off + 27], G._POS)
    assert x == pytest.approx(_clamp(rrec["x_m"], G._POS, 27), rel=1e-6, abs=1.0)


def test_stream_is_deterministic(rrec):
    a, _ = G.nav_stream(rrec, 0, 561600.0, 6)
    b, _ = G.nav_stream(rrec, 0, 561600.0, 6)
    assert np.array_equal(a, b)


def _clamp(v, scale, n):
    q = int(round(v / scale))
    q = max(-(1 << (n - 1)) + 1, min((1 << (n - 1)) - 1, q))
    return q * scale
