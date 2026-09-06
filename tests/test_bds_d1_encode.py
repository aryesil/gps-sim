"""SP-D: BeiDou B1I D1/D2 encoder -- coding layer + clean field round-trip."""
import numpy as np
import pytest

from backend.analysis import bds_d1_encode as B
from backend.ephem import ephemeris

_WEEK, _SOW = 910, 561600.0


@pytest.fixture(scope="module")
def crec():
    e = ephemeris.parse_rinex_multi("tests/fixtures/brdc_mixed.rnx", ("C",),
                                    require=())
    rec = ephemeris.align_epochs(e, _WEEK, _SOW)
    rec = [v for k, v in rec.items() if k[0] == "C"][0]
    rec["prn"] = 6
    return rec


def test_bch_is_systematic_and_length_15():
    w = B.bch_encode([1, 0, 1, 1, 0, 0, 0, 1, 0, 0, 1])
    assert len(w) == 15
    assert w[:11] == [1, 0, 1, 1, 0, 0, 0, 1, 0, 0, 1]
    assert B.bch_encode([0] * 11) == [0] * 15


def test_interleave_is_bit_alternating():
    il = B.interleave2(list(range(15)), list(range(100, 115)))
    assert il[:6] == [0, 100, 1, 101, 2, 102]
    a, b = B.deinterleave2(il)
    assert a == list(range(15)) and b == list(range(100, 115))


def test_subframe_layout(crec):
    sf = B.build_subframe(2, crec, int(_SOW), _WEEK)
    assert len(sf) == 300
    assert sf[:11] == B.PREAMBLE
    d = B.decode_subframe(sf)
    assert d["fra_id"] == 2
    assert len(d["info"]) == 189


def test_sf2_fields_round_trip_clean(crec):
    d = B.decode_subframe(B.build_subframe(2, crec, int(_SOW), _WEEK))
    info = d["info"]
    # SF2 contiguous layout: dn16 cuc18 M0_32 e32 cus18 crc18 crs18 sqrtA32 toe17
    off = 16 + 18 + 32 + 32 + 18 + 18 + 18
    sqrta = _u(info[off:off + 32]) * (2 ** -19)
    e = _u(info[16 + 18 + 32:16 + 18 + 32 + 32]) * (2 ** -33)
    assert sqrta == pytest.approx(crec["sqrtA"], abs=1e-3)
    assert e == pytest.approx(crec["e"], abs=1e-9)


def test_d1_and_d2_rates(crec):
    a1, r1 = B.nav_stream(crec, _WEEK, _SOW, 6)
    a2, r2 = B.nav_stream(crec, _WEEK, _SOW, 6, d2=True)
    assert r1 == 50.0 and r2 == 500.0
    assert sorted(set(a1.tolist())) == [-1, 1]
    frames = B.decode_frame(a1)
    assert {1, 2, 3} <= set(frames)


def _u(bits):
    return int("".join(map(str, bits)), 2)
