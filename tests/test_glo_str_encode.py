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
    # data[0:4] is the string number "m" (a real ICD field, not a decoder
    # hack); string 1 payload starts right after it:
    # [tk12][vx s-m 24][ax s-m 5][x_m s-m 27].
    d1 = frame[1]
    off = 4 + 12 + 24 + 5
    x = G._from_sign_mag(d1[off:off + 27], G._POS)
    assert x == pytest.approx(_clamp(rrec["x_m"], G._POS, 27), rel=1e-6, abs=1.0)


def test_string_fields_round_trip_health_vy_gamma_tau(rrec):
    """Regression: the string number used to be overwritten into data[:4]
    post-hoc, destroying string 2/3/4's leading field (health's only bit,
    vy/gamma/tau's sign bit + top magnitude bits, since each of those fields
    starts right at data-bit 0). Now it's prepended instead, so these must
    round-trip intact."""
    rec = dict(rrec)
    rec["health"] = 1
    rec["gamma"] = -1.25e-11
    rec["tau"] = -3.2e-4
    arr, _r = G.nav_stream(rec, 0, 561600.0, 6)
    frame = G.decode_frame(arr)
    assert {2, 3, 4} <= set(frame)

    d2 = frame[2]
    assert d2[4] == 1                                       # B_n (health)
    off = 4 + 1 + 24 + 5
    y = G._from_sign_mag(d2[off:off + 27], G._POS)
    assert y == pytest.approx(_clamp(rec["y_m"], G._POS, 27), rel=1e-6, abs=1.0)

    d3 = frame[3]
    gamma = G._from_sign_mag(d3[4:4 + 11], G._GAM)
    assert gamma == pytest.approx(_clamp(rec["gamma"], G._GAM, 11), rel=1e-6)

    d4 = frame[4]
    tau = G._from_sign_mag(d4[4:4 + 22], G._TAU)
    assert tau == pytest.approx(_clamp(rec["tau"], G._TAU, 22), rel=1e-6)


def test_reconstruct_ephemeris_round_trips_into_glonass_state(rrec):
    """SP-5: decode_frame's output must invert back into a record that
    glonass.glonass_state accepts and propagates correctly at t==toe_ref."""
    from backend.synth import glonass

    rec = dict(rrec)
    rec["health"] = 0
    arr, _r = G.nav_stream(rec, 0, 561600.0, 6)
    frame = G.decode_frame(arr)
    assert {1, 2, 3, 4} <= set(frame)

    got = G.reconstruct_ephemeris(frame, toe_ref=561600.0)
    for key in ("x_m", "y_m", "z_m", "vx", "vy", "vz", "ax", "ay", "az",
               "tau", "gamma"):
        assert got[key] == pytest.approx(_clamp_field(rec, key), rel=1e-6,
                                         abs=1.0)
    assert got["health"] == 0
    assert got["glo_k"] == int(rec["glo_k"]) & 0x1F

    f = glonass.glonass_state(got)
    pos, vel, _clk = f(561600.0)
    assert pos == pytest.approx([got["x_m"], got["y_m"], got["z_m"]], abs=1.0)
    assert vel == pytest.approx([got["vx"], got["vy"], got["vz"]], abs=1e-3)


def _clamp_field(rec, key):
    scale, n = {"x_m": (G._POS, 27), "y_m": (G._POS, 27), "z_m": (G._POS, 27),
                "vx": (G._VEL, 24), "vy": (G._VEL, 24), "vz": (G._VEL, 24),
                "ax": (G._ACC, 5), "ay": (G._ACC, 5), "az": (G._ACC, 5),
                "tau": (G._TAU, 22), "gamma": (G._GAM, 11)}[key]
    return _clamp(rec.get(key, 0.0), scale, n)


def test_stream_is_deterministic(rrec):
    a, _ = G.nav_stream(rrec, 0, 561600.0, 6)
    b, _ = G.nav_stream(rrec, 0, 561600.0, 6)
    assert np.array_equal(a, b)


def _clamp(v, scale, n):
    q = int(round(v / scale))
    q = max(-(1 << (n - 1)) + 1, min((1 << (n - 1)) - 1, q))
    return q * scale
