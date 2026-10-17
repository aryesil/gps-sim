import math

import pytest

from backend.analysis import lnav_encode as le
from backend.analysis import lnav_decode as ld
from backend.ephem import ephemeris

_HDR = {"iono_alpha": [1.1e-8, 0.0, -5.96e-8, 0.0],
        "iono_beta": [88064.0, 0.0, -196608.0, 0.0],
        "utc": {"A0": 1.86e-9, "A1": 3.5e-15}}


def _one_eph():
    eph = ephemeris.parse_rinex("tests/fixtures/brdc_sample.rnx")
    prn = sorted(eph)[0]
    return ephemeris.align_epochs({prn: eph[prn]}, 200, 100800.0)[prn]


def test_check_parity_accepts_encoder_output():
    sf = le.subframe1(_one_eph(), week=200, tow_count=100)
    d29 = d30 = 0
    for i in range(10):
        w = sf[i * 30:(i + 1) * 30]
        assert ld.check_parity(w, d29, d30)
        d29, d30 = w[28], w[29]


def test_find_frame_locates_all_five_subframes():
    f = le.frame_bits(_one_eph(), _HDR, 200, 100, 1)
    hits = ld.find_frame(f + f)
    ids = [h["subframe_id"] for h in hits]
    assert ids[:5] == [1, 2, 3, 4, 5]


def test_find_frame_handles_inverted_stream():
    f = le.frame_bits(_one_eph(), _HDR, 200, 100, 1)
    inv = [1 - b for b in f]
    hits = ld.find_frame(inv)
    assert hits and hits[0]["inverted"] and hits[0]["subframe_id"] == 1


def test_decode_ephemeris_roundtrips_within_scale_lsb():
    eph = _one_eph()
    f = le.frame_bits(eph, _HDR, week=200, tow_count=100, sf45_page=1)
    hits = ld.find_frame(f)
    sfs = {h["subframe_id"]: h["words"] for h in hits}
    got = ld.decode_ephemeris(sfs)
    for k, scale in [("sqrtA", 2 ** -19), ("e", 2 ** -33), ("toe", 16),
                     ("af0", 2 ** -31), ("crs", 2 ** -5), ("cuc", 2 ** -29),
                     ("toc", 16), ("tgd", 2 ** -31)]:
        assert abs(got[k] - eph[k]) <= 2 * scale, k
    for k in ("m0", "omega0", "i0", "omega", "delta_n", "omega_dot", "idot"):
        assert abs(got[k] - eph[k]) <= 4 * math.pi * 2 ** -31, k
    assert got["iode"] == int(eph["iode"]) & 0xFF
    assert got["gps_week"] == 200 & 0x3FF


def test_decode_ephemeris_needs_sf123():
    with pytest.raises(ValueError):
        ld.decode_ephemeris({1: [[0] * 30] * 10})
