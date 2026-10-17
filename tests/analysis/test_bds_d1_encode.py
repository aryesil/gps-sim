"""SP-D: BeiDou B1I D1/D2 encoder -- coding layer + ICD field layout.

The layout checks read the ICD-ordered subframe at RTKLIB's
``decode_bds_d1`` / ``decode_bds_d2`` bit positions (rcvraw.c), an
implementation independent of this encoder.
"""
import math

import numpy as np
import pytest

from backend.analysis import bds_d1_encode as B
from backend.ephem import ephemeris

_WEEK, _SOW = 910, 561600.0
_IONO = ((1.1e-8, 1.5e-8, -6.0e-8, -1.2e-7), (1.0e5, 1.3e5, -6.5e4, -4.0e5))
_SC = math.pi


@pytest.fixture(scope="module")
def crec():
    e = ephemeris.parse_rinex_multi("tests/fixtures/brdc_mixed.rnx", ("C",),
                                    require=())
    rec = ephemeris.align_epochs(e, _WEEK, _SOW)
    rec = dict([v for k, v in rec.items() if k[0] == "C"][0])
    rec["prn"] = 6
    rec["toe"] = rec["toc"] = 561600.0 - 14.0 - (561600.0 - 14.0) % 8
    return rec


# -- RTKLIB-style bit access over the ICD-ordered 300-bit subframe ----------
def _u(b, p, n):
    return int("".join(map(str, b[p:p + n])), 2)


def _s(b, p, n):
    v = _u(b, p, n)
    return v - (1 << n) if v >> (n - 1) else v


def _u2(b, p1, l1, p2, l2):
    return (_u(b, p1, l1) << l2) | _u(b, p2, l2)


def _s2(b, p1, l1, p2, l2):
    v = _u2(b, p1, l1, p2, l2)
    n = l1 + l2
    return v - (1 << n) if v >> (n - 1) else v


def _s3(b, p1, l1, p2, l2, p3, l3):
    v = (_u2(b, p1, l1, p2, l2) << l3) | _u(b, p3, l3)
    n = l1 + l2 + l3
    return v - (1 << n) if v >> (n - 1) else v


def _merge_s(a, b, n):
    return a * (1 << n) + b


def test_bch_is_systematic_and_length_15():
    w = B.bch_encode([1, 0, 1, 1, 0, 0, 0, 1, 0, 0, 1])
    assert len(w) == 15
    assert w[:11] == [1, 0, 1, 1, 0, 0, 0, 1, 0, 0, 1]
    assert B.bch_encode([0] * 11) == [0] * 15


def test_bch_parity_divides_generator():
    # every codeword is a multiple of g(x) = x^4 + x + 1
    rng = np.random.default_rng(3)
    for _ in range(20):
        cw = B.bch_encode(rng.integers(0, 2, 11).tolist())
        r = int("".join(map(str, cw)), 2)
        for sh in range(14, 3, -1):
            if r >> sh & 1:
                r ^= 0b10011 << (sh - 4)
        assert r == 0


def test_interleave_is_bit_alternating():
    il = B.interleave2(list(range(15)), list(range(100, 115)))
    assert il[:6] == [0, 100, 1, 101, 2, 102]
    a, b = B.deinterleave2(il)
    assert a == list(range(15)) and b == list(range(100, 115))


def test_word1_header_at_icd_positions(crec):
    sow = int(_SOW) - 14 + 6
    b = B.icd_order(B.d1_subframe(2, crec, sow, _WEEK))
    assert b[:11] == B.PREAMBLE
    assert _u(b, 11, 4) == 0                       # Rev
    assert _u(b, 15, 3) == 2                       # FraID
    assert _u2(b, 18, 8, 30, 12) == sow            # SOW 8 + 12
    assert B.decode_subframe(B.d1_subframe(2, crec, sow, _WEEK))["bch_ok"]


def test_d1_ephemeris_matches_rtklib_positions(crec):
    sow = 561594
    sf = [B.icd_order(B.d1_subframe(k, crec, sow + 6 * (k - 1), _WEEK, _IONO))
          for k in (1, 2, 3)]
    b1, b2, b3 = sf
    r = crec
    assert [_u(b, 15, 3) for b in sf] == [1, 2, 3]
    assert _u(b1, 60, 13) == _WEEK
    assert _u2(b1, 73, 9, 90, 8) * 8.0 == r["toc"]
    assert _s(b1, 98, 10) * 0.1e-9 == pytest.approx(r["tgd"], abs=1e-10)
    assert _s2(b1, 108, 4, 120, 6) * 0.1e-9 == pytest.approx(r["tgd2"], abs=1e-10)
    assert _s(b1, 214, 11) * 2 ** -66 == pytest.approx(r["af2"], abs=2 ** -66)
    assert _s2(b1, 225, 7, 240, 17) * 2 ** -33 == pytest.approx(r["af0"], abs=2 ** -33)
    assert _s2(b1, 257, 5, 270, 17) * 2 ** -50 == pytest.approx(r["af1"], abs=2 ** -50)
    assert _u(b1, 287, 5) == int(r["iode"]) & 0x1F
    # SF1 iono (decode_bds_d1_ion)
    assert _s(b1, 126, 8) * 2 ** -30 == pytest.approx(_IONO[0][0], abs=2 ** -30)
    assert _s(b1, 158, 8) * 2 ** -24 == pytest.approx(_IONO[0][3], abs=2 ** -24)
    assert _s2(b1, 166, 6, 180, 2) * 2 ** 11 == pytest.approx(_IONO[1][0], abs=2 ** 11)
    assert _s2(b1, 198, 4, 210, 4) * 2 ** 16 == pytest.approx(_IONO[1][3], abs=2 ** 16)

    assert _s2(b2, 42, 10, 60, 6) * 2 ** -43 * _SC == pytest.approx(r["delta_n"], rel=1e-4)
    assert _s2(b2, 66, 16, 90, 2) * 2 ** -31 == pytest.approx(r["cuc"], abs=2 ** -31)
    assert _s2(b2, 92, 20, 120, 12) * 2 ** -31 * _SC == pytest.approx(r["m0"], abs=1e-8)
    assert _u2(b2, 132, 10, 150, 22) * 2 ** -33 == pytest.approx(r["e"], abs=2 ** -33)
    assert _s(b2, 180, 18) * 2 ** -31 == pytest.approx(r["cus"], abs=2 ** -31)
    assert _s2(b2, 198, 4, 210, 14) * 2 ** -6 == pytest.approx(r["crc"], abs=2 ** -6)
    assert _s2(b2, 224, 8, 240, 10) * 2 ** -6 == pytest.approx(r["crs"], abs=2 ** -6)
    assert _u2(b2, 250, 12, 270, 20) * 2 ** -19 == pytest.approx(r["sqrtA"], abs=2 ** -19)

    toe = ((_u(b2, 290, 2) << 15) | _u2(b3, 42, 10, 60, 5)) * 8.0
    assert toe == r["toe"]
    assert _s2(b3, 65, 17, 90, 15) * 2 ** -31 * _SC == pytest.approx(r["i0"], abs=1e-8)
    assert _s2(b3, 105, 7, 120, 11) * 2 ** -31 == pytest.approx(r["cic"], abs=2 ** -31)
    assert _s2(b3, 131, 11, 150, 13) * 2 ** -43 * _SC == pytest.approx(r["omega_dot"], rel=1e-4)
    assert _s2(b3, 163, 9, 180, 9) * 2 ** -31 == pytest.approx(r["cis"], abs=2 ** -31)
    assert _s2(b3, 189, 13, 210, 1) * 2 ** -43 * _SC == pytest.approx(r["idot"], abs=1e-12)
    assert _s2(b3, 211, 21, 240, 11) * 2 ** -31 * _SC == pytest.approx(r["omega0"], abs=1e-8)
    assert _s2(b3, 251, 11, 270, 21) * 2 ** -31 * _SC == pytest.approx(r["omega"], abs=1e-8)


def test_d2_ephemeris_matches_rtklib_positions(crec):
    fs0 = 561570                                   # 30 s aligned: page 1
    pg = {p: B.icd_order(B.d2_subframe(1, crec, fs0 + 3 * (p - 1), _WEEK, _IONO))
          for p in range(1, 11)}
    r = crec
    assert [_u(pg[p], 42, 4) for p in range(1, 11)] == list(range(1, 11))
    assert all(_u(pg[p], 15, 3) == 1 for p in pg)
    assert _u2(pg[1], 18, 8, 30, 12) == fs0
    b = pg[1]
    assert _u(b, 64, 13) == _WEEK
    assert _u2(b, 77, 5, 90, 12) * 8.0 == r["toc"]
    assert _s(b, 102, 10) * 0.1e-9 == pytest.approx(r["tgd"], abs=1e-10)
    assert _s(b, 120, 10) * 0.1e-9 == pytest.approx(r["tgd2"], abs=1e-10)
    f0 = _s2(pg[3], 100, 12, 120, 12) * 2 ** -33
    f1 = _merge_s(_s(pg[3], 132, 4), _u2(pg[4], 46, 6, 60, 12), 18) * 2 ** -50
    assert f0 == pytest.approx(r["af0"], abs=2 ** -33)
    assert f1 == pytest.approx(r["af1"], abs=2 ** -50)
    b = pg[4]
    assert _s2(b, 72, 10, 90, 1) * 2 ** -66 == pytest.approx(r["af2"], abs=2 ** -66)
    assert _u(b, 91, 5) == int(r["iode"]) & 0x1F
    assert _s(b, 96, 16) * 2 ** -43 * _SC == pytest.approx(r["delta_n"], rel=1e-4)
    cuc = _merge_s(_s(pg[4], 120, 14), _u(pg[5], 46, 4), 4) * 2 ** -31
    assert cuc == pytest.approx(r["cuc"], abs=2 ** -31)
    b = pg[5]
    assert _s3(b, 50, 2, 60, 22, 90, 8) * 2 ** -31 * _SC == pytest.approx(r["m0"], abs=1e-8)
    assert _s2(b, 98, 14, 120, 4) * 2 ** -31 == pytest.approx(r["cus"], abs=2 ** -31)
    e = _merge_s(_u(pg[5], 124, 10), _u2(pg[6], 46, 6, 60, 16), 22) * 2 ** -33
    assert e == pytest.approx(r["e"], abs=2 ** -33)
    sqrta = ((_u2(pg[6], 76, 6, 90, 22) << 4) | _u(pg[6], 120, 4)) * 2 ** -19
    assert sqrta == pytest.approx(r["sqrtA"], abs=2 ** -19)
    cic = _merge_s(_s(pg[6], 124, 10), _u2(pg[7], 46, 6, 60, 2), 8) * 2 ** -31
    assert cic == pytest.approx(r["cic"], abs=2 ** -31)
    b = pg[7]
    assert _s(b, 62, 18) * 2 ** -31 == pytest.approx(r["cis"], abs=2 ** -31)
    assert _u2(b, 80, 2, 90, 15) * 8.0 == r["toe"]
    i0 = _merge_s(_s2(pg[7], 105, 7, 120, 14), _u2(pg[8], 46, 6, 60, 5), 11)
    assert i0 * 2 ** -31 * _SC == pytest.approx(r["i0"], abs=1e-8)
    b = pg[8]
    assert _s2(b, 65, 17, 90, 1) * 2 ** -6 == pytest.approx(r["crc"], abs=2 ** -6)
    assert _s(b, 91, 18) * 2 ** -6 == pytest.approx(r["crs"], abs=2 ** -6)
    omd = _merge_s(_s2(pg[8], 109, 3, 120, 16), _u(pg[9], 46, 5), 5)
    assert omd * 2 ** -43 * _SC == pytest.approx(r["omega_dot"], rel=1e-4)
    b = pg[9]
    assert _s3(b, 51, 1, 60, 22, 90, 9) * 2 ** -31 * _SC == pytest.approx(r["omega0"], abs=1e-8)
    om = _merge_s(_s2(pg[9], 99, 13, 120, 14), _u(pg[10], 46, 5), 5)
    assert om * 2 ** -31 * _SC == pytest.approx(r["omega"], abs=1e-8)
    assert _s2(pg[10], 51, 1, 60, 13) * 2 ** -43 * _SC == pytest.approx(r["idot"], abs=1e-12)


def test_d1_and_d2_rates(crec):
    a1, r1 = B.nav_stream(crec, _WEEK, _SOW, 6)
    a2, r2 = B.nav_stream(crec, _WEEK, _SOW, 6, d2=True)
    assert r1 == 50.0 and r2 == 500.0
    assert sorted(set(a1.tolist())) == [-1, 1]
    assert {1, 2, 3, 4, 5} <= set(B.decode_frame(a1))
    assert {1, 2, 3, 4, 5} <= set(B.decode_frame(a2))


def test_d2_frame_carries_frame_sow(crec):
    a, _ = B.nav_stream(crec, _WEEK, 561571.2, 3, d2=True)
    bits = (a > 0).astype(int).tolist()
    sows = [B.decode_subframe(bits[k * 300:(k + 1) * 300])["sow"]
            for k in range(10)]
    assert sows == [561570] * 5 + [561573] * 5


def test_d1_subframe_follows_bdt(crec):
    # a stream starting at SOW 561612 begins with subframe (561612/6)%5+1 = 3
    a, _ = B.nav_stream(crec, _WEEK, 561613.0, 6)
    d = B.decode_subframe((a[:300] > 0).astype(int).tolist())
    assert (d["fra_id"], d["sow"]) == (3, 561612)
