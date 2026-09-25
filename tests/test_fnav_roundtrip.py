import numpy as np
import pytest

from backend.analysis import fnav_decode as D
from backend.analysis import fnav_encode as F
from backend.ephem import ephemeris

_RINEX = "tests/fixtures/brdc_mixed.rnx"
_WEEK, _SOW = 2434, 194440.0


@pytest.fixture(scope="module")
def gal_rec():
    e = ephemeris.parse_rinex_multi(_RINEX, ("E",), require=())
    e = ephemeris.align_epochs(e, _WEEK, _SOW)
    rec = [v for k, v in e.items() if k[0] == "E"][0]
    rec["prn"] = 2
    return rec


def _sym01(arr):
    return [0 if s == 1 else 1 for s in arr.tolist()]


def test_conv_roundtrip_bit_exact():
    data = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 1, 0] * 20 + [0] * 4
    assert D.viterbi_free_decode(F.conv_encode(data)) == data


def test_message_frames_survive_crc(gal_rec):
    prn = gal_rec["prn"]
    arr, _ = F.nav_stream(gal_rec, prn, 1325, _SOW, 60)
    msgs = D.decode_messages(_sym01(arr))
    types = {m["type"] for m in msgs if m["crc_ok"]}
    assert {1, 2, 3, 4} <= types
    assert all(m["prn"] == prn for m in msgs if m["crc_ok"] and m["type"] == 1)


def test_ephemeris_reconstruction_roundtrip(gal_rec):
    prn = gal_rec["prn"]
    arr, _ = F.nav_stream(gal_rec, prn, 1325, _SOW, 60)
    rec = D.reconstruct_ephemeris(D.decode_messages(_sym01(arr)))
    assert rec["e"] == pytest.approx(gal_rec["e"], abs=1e-9)
    assert rec["sqrtA"] == pytest.approx(gal_rec["sqrtA"], abs=1e-3)
    assert rec["m0"] == pytest.approx(gal_rec["m0"], abs=1e-8)
    assert rec["omega0"] == pytest.approx(gal_rec["omega0"], abs=1e-8)
    assert rec["omega"] == pytest.approx(gal_rec["omega"], abs=1e-8)
    assert rec["i0"] == pytest.approx(gal_rec["i0"], abs=1e-8)
    assert rec["af0"] == pytest.approx(gal_rec["af0"], abs=1e-9)
    # F/NAV carries BGD(E1,E5a)
    assert rec["tgd"] == pytest.approx(gal_rec.get("tgd_e5a", 0.0), abs=1e-10)
    assert rec["toe"] == pytest.approx(gal_rec["toe"], abs=60.0)
    assert rec["cic"] == pytest.approx(gal_rec.get("cic", 0.0), abs=1e-9)


def test_reconstruction_needs_1_2_and_3():
    with pytest.raises(ValueError):
        D.reconstruct_ephemeris([{"type": 4, "crc_ok": True, "fields": {}}])
