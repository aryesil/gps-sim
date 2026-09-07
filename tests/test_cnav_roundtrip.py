import numpy as np
import pytest

from backend.analysis import cnav_decode as D
from backend.analysis import cnav_encode as C
from backend.ephem import ephemeris

_RINEX = "tests/fixtures/brdc_full.rnx"
_WEEK, _SOW = 2187, 561600.0


@pytest.fixture(scope="module")
def gps_rec():
    e = ephemeris.parse_rinex_multi(_RINEX, ("G",), require=())
    e = ephemeris.align_epochs(e, _WEEK, _SOW)
    for v in e.values():
        if isinstance(v, dict):
            v["gps_week"] = _WEEK
            return v
    raise AssertionError


def _sym01(arr):
    return [0 if s == 1 else 1 for s in arr.tolist()]


def test_conv_roundtrip_bit_exact():
    data = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 1, 0] * 25 + [0] * 6
    assert D.viterbi_free_decode(C._Conv().encode(data)) == data


def test_message_frames_survive_crc(gps_rec):
    arr, _ = C.nav_stream(gps_rec, {}, _WEEK, _SOW, 60, prn=1)
    msgs = D.decode_messages(_sym01(arr))
    types = {m["type"] for m in msgs if m["crc_ok"]}
    assert {10, 11, 30} <= types
    assert all(m["prn"] == 1 for m in msgs if m["crc_ok"])


def test_ephemeris_reconstruction_roundtrip(gps_rec):
    hdr = {"iono_alpha": [1e-8, 0, 0, 0], "iono_beta": [1.4e5, 0, 0, 0]}
    arr, _ = C.nav_stream(gps_rec, hdr, _WEEK, _SOW, 60, prn=1)
    rec = D.reconstruct_ephemeris(D.decode_messages(_sym01(arr)))
    assert rec["e"] == pytest.approx(gps_rec["e"], abs=1e-9)
    assert rec["sqrtA"] == pytest.approx(gps_rec["sqrtA"], abs=1e-2)
    assert rec["m0"] == pytest.approx(gps_rec["m0"], abs=1e-8)
    assert rec["omega0"] == pytest.approx(gps_rec["omega0"], abs=1e-8)
    assert rec["omega"] == pytest.approx(gps_rec["omega"], abs=1e-8)
    assert rec["i0"] == pytest.approx(gps_rec["i0"], abs=1e-8)
    assert rec["af0"] == pytest.approx(gps_rec["af0"], abs=1e-9)
    assert rec["toe"] == pytest.approx(gps_rec["toe"], abs=300.0)


def test_reconstruction_needs_10_and_11():
    with pytest.raises(ValueError):
        D.reconstruct_ephemeris([{"type": 30, "crc_ok": True, "fields": {}}])
