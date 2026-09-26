import numpy as np
import pytest

from backend.analysis import cnav_encode as C
from backend.analysis._crc import crc24q
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
    raise AssertionError("no G record")


def test_conv_encode_reference_vectors():
    assert C._Conv().encode([1]) == [1, 0]
    assert C._Conv().encode([1, 0]) == [1, 0, 0, 0]
    assert C._Conv().encode([0, 0, 0]) == [0, 1, 0, 1, 0, 1]


def test_conv_is_rate_half():
    out = C._Conv().encode([1, 0, 1, 1, 0, 0, 1, 0] * 5)
    assert len(out) == 2 * 40


def test_crc24q_zero_bits_is_zero():
    assert crc24q([0] * 200) == [0] * 24


def test_crc24q_detects_single_bit_flip():
    a = crc24q([1, 0, 1, 1] * 50)
    b = crc24q([0, 0, 1, 1] + [1, 0, 1, 1] * 49)
    assert a != b


def test_message_layout_preamble_prn_type_tow():
    msg = C.build_message(10, 7, 100800 // 6, 0, {}, {})
    assert len(msg) == 300
    assert msg[:8] == [1, 0, 0, 0, 1, 0, 1, 1]          # 0x8B
    assert msg[8:14] == [0, 0, 0, 1, 1, 1]              # PRN 7
    assert msg[14:20] == [0, 0, 1, 0, 1, 0]             # type 10
    assert msg[20:37] == [int(b) for b in f"{100800 // 6:017b}"]
    assert msg[37] == 0                                 # alert
    assert crc24q(msg[:276]) == msg[276:300]


@pytest.mark.parametrize("mtype", [10, 11, 30, 33])
def test_every_message_type_is_300_bits_with_valid_crc(gps_rec, mtype):
    hdr = {"iono_alpha": [1e-8, 0, 0, 0], "iono_beta": [1.4e5, 0, 0, 0]}
    msg = C.build_message(mtype, 1, 0, 0, gps_rec, hdr)
    assert len(msg) == 300
    assert crc24q(msg[:276]) == msg[276:300]


def test_nav_stream_rate_and_alphabet(gps_rec):
    arr, rate = C.nav_stream(gps_rec, {}, _WEEK, _SOW, 30, prn=1)
    assert rate == 50.0
    assert arr.dtype == np.int8
    assert set(np.unique(arr)).issubset({-1, 1})
    assert len(arr) % 600 == 0            # 600 symbols per 300-bit message


def test_l5_rate_stream_steps_tow_and_type_every_6_s(gps_rec):
    from backend.analysis import cnav_decode as D
    arr, rate = C.nav_stream(gps_rec, {}, _WEEK, _SOW, 30, prn=1,
                             sym_rate=100.0)
    assert rate == 100.0
    msgs = [m for m in D.decode_messages([1 if s < 0 else 0 for s in arr])
            if m["crc_ok"]]
    assert len(msgs) >= 4
    tows = [m["tow_6s"] for m in msgs]
    assert all(b - a == 1 for a, b in zip(tows, tows[1:]))     # 6 s units
    types = [m["type"] for m in msgs]
    assert {10, 11, 30, 33} <= set(types[:4])
