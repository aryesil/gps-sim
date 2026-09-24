"""SP-D: GLONASS L1OF/L2OF string encoder (ICD 5.1 framing)."""
import numpy as np
import pytest

from backend.analysis import glo_str_encode as G
from backend.ephem import ephemeris

_WEEK = 2325
_SOW = 561618.0     # GPS 2024-08-03 12:00:18 = UTC 12:00:00 = Moscow 15:00:00


@pytest.fixture(scope="module")
def rrec():
    e = ephemeris.parse_rinex_multi("tests/fixtures/brdc_mixed.rnx", ("R",),
                                    require=())
    key = [k for k in e if k[0] == "R"][0]
    rec = ephemeris.align_epochs({key: e[key]}, _WEEK, _SOW,
                                 kepler_grid_s=3600.0)[key]
    rec["prn"] = key[1]
    return rec


def test_string_is_200_symbols_meander_then_time_mark(rrec):
    a, rate = G.nav_stream(rrec, _WEEK, _SOW, 2)
    assert rate == 100.0
    s = a[:200]
    assert np.all(s[:170].reshape(-1, 2).sum(axis=1) == 0)     # meander pairs
    mark = np.array([-1 if c else 1 for c in G._TIME_MARK])
    assert np.array_equal(s[170:200], mark)


def test_hamming_zero_string_and_single_bit_error_detected():
    s = G.hamming_string([0] * 76)
    assert len(s) == 85 and G.hamming_check(s)
    for i in range(1, 85):
        bad = list(s)
        bad[i] ^= 1
        assert not G.hamming_check(bad)


def test_hamming_matches_icd_check_equations():
    # ICD 5.1: C_1 = beta_1 xor b9 xor b10 xor b12 ... must be 0; bit n of
    # the string is at transmission index 85 - n.
    rng = np.random.default_rng(1)
    s = G.hamming_string(rng.integers(0, 2, 76).tolist())
    b = {n: s[85 - n] for n in range(1, 86)}
    c1 = b[1]
    for n in (9, 10, 12, 13, 15, 17, 19, 20, 22, 24, 26, 28, 30, 32, 34, 35,
              37, 39, 41, 43, 45, 47, 49, 51, 53, 55, 57, 59, 61, 63, 65, 66,
              68, 70, 72, 74, 76, 78, 80, 82, 84):
        c1 ^= b[n]
    assert c1 == 0
    assert sum(b.values()) % 2 == 0          # C_sigma


def test_strings_follow_moscow_time(rrec):
    # Moscow 15:00:00 is a frame start: string 1 first, t_k = 15:00:00.
    a, _ = G.nav_stream(rrec, _WEEK, _SOW, 30)
    ids = [G.decode_frame(a[i * 200:(i + 1) * 200]) for i in range(15)]
    assert [next(iter(d)) for d in ids] == list(range(1, 16))
    got = G.reconstruct_ephemeris(ids[0], toe_ref=_SOW)
    assert got["tk_s"] == 15 * 3600
    # a stream starting 8 s later begins with string 5
    b, _ = G.nav_stream(rrec, _WEEK, _SOW + 8, 2)
    assert set(G.decode_frame(b[:200])) == {5}


def test_carrier_sign_flip_decodes_identically(rrec):
    a, _ = G.nav_stream(rrec, _WEEK, _SOW, 30)
    assert G.decode_frame(a) == G.decode_frame(-a)


def test_reconstruct_ephemeris_round_trips_into_glonass_state(rrec):
    from backend.synth import glonass

    rec = dict(rrec)
    rec["health"] = 0
    rec["gamma"] = -1.25e-11
    arr, _r = G.nav_stream(rec, _WEEK, _SOW, 8)
    frame = G.decode_frame(arr)
    assert {1, 2, 3, 4} <= set(frame)

    got = G.reconstruct_ephemeris(frame, toe_ref=rec["toe_ref"], glo_k=3)
    for key in ("x_m", "y_m", "z_m", "vx", "vy", "vz", "ax", "ay", "az",
                "tau", "gamma"):
        assert got[key] == pytest.approx(_clamp_field(rec, key), rel=1e-6,
                                         abs=1.0)
    assert got["health"] == 0
    assert got["glo_k"] == 3
    assert got["slot"] == rec["prn"]
    assert got["tb"] == 60                   # 15:00 Moscow = 60 x 15 min
    assert got["nt"] == 216                  # 2024-08-03: day 216 of 2024..2027

    f = glonass.glonass_state(got)
    pos, vel, _clk = f(rec["toe_ref"])
    assert pos == pytest.approx([got["x_m"], got["y_m"], got["z_m"]], abs=1.0)
    assert vel == pytest.approx([got["vx"], got["vy"], got["vz"]], abs=1e-3)


def test_tau_is_broadcast_as_icd_taun(rrec):
    # records hold RINEX's clock bias (-TauN); string 4 carries TauN
    rec = dict(rrec)
    rec["tau"] = 1.5e-4
    arr, _ = G.nav_stream(rec, _WEEK, _SOW, 8)
    s4 = [0] + G.decode_frame(arr)[4] + [0] * 8
    assert G._get_sm(s4, 6, 22, G._TAU) == pytest.approx(-1.5e-4, abs=G._TAU)


def _clamp_field(rec, key):
    scale, n = {"x_m": (G._POS, 27), "y_m": (G._POS, 27), "z_m": (G._POS, 27),
                "vx": (G._VEL, 24), "vy": (G._VEL, 24), "vz": (G._VEL, 24),
                "ax": (G._ACC, 5), "ay": (G._ACC, 5), "az": (G._ACC, 5),
                "tau": (G._TAU, 22), "gamma": (G._GAM, 11)}[key]
    return _clamp(rec.get(key, 0.0), scale, n)


def test_stream_is_deterministic(rrec):
    a, _ = G.nav_stream(rrec, _WEEK, _SOW, 6)
    b, _ = G.nav_stream(rrec, _WEEK, _SOW, 6)
    assert np.array_equal(a, b)


def _clamp(v, scale, n):
    q = int(round(v / scale))
    q = max(-(1 << (n - 1)) + 1, min((1 << (n - 1)) - 1, q))
    return q * scale


def test_toe_ref_from_tb_resolves_nearest_epoch(rrec):
    assert G.toe_ref_from_tb(60, _SOW + 200.0) == pytest.approx(_SOW)
    assert G.toe_ref_from_tb(61, _SOW + 10.0) == pytest.approx(_SOW + 900.0)
