"""BeiDou B1I ranging code + Neumann-Hoffman secondary (synth_code case 3).

Known-answer tests pin the committed G2 phase-assignment table
(backend/synth/native/b1i_taps.hpp) so an accidental table edit or a
reversed/omitted tap fails loudly. Reference implementation cross-checked
against GNSS-SDR `beidou_b1i_signal_replica.cc` (phase1/phase2/phase3
arrays) and BDS-SIS-ICD-B1I v3.0 Table 5-6 (PRN 1..37 == v2.0 Table 4-2).
Chip map matches the GPS path: code bit 0 -> +1, code bit 1 -> -1.
"""
import hashlib

import numpy as np
import pytest

from backend.synth import _lib

_NH20 = np.array([-1, -1, -1, -1, -1, 1, -1, -1, 1, 1,
                  -1, 1, -1, 1, -1, -1, 1, 1, 1, -1], np.int8)


def test_b1i_primary_period_and_balance():
    p, s = _lib.code(3, 6, 2046, 20)
    assert p.size == 2046
    assert set(np.unique(p).tolist()) <= {-1, 1}
    assert abs(int(p.sum())) <= 100
    assert np.array_equal(s, _NH20)


def test_b1i_geo_secondary_is_flat():
    p, s = _lib.code(3, 1, 2046, 20)      # C01 is GEO
    assert np.array_equal(s, np.ones(20, np.int8))


def test_b1i_geo_secondary_is_flat_high_prn():
    # C59..C63 (PRN 59..63) are also GEO in the ICD 3.0 numbering.
    for prn in (59, 60, 61, 62, 63):
        _p, s = _lib.code(3, prn, 2046, 20)
        assert np.array_equal(s, np.ones(20, np.int8)), prn


def test_b1i_meo_secondary_is_nh():
    for prn in (6, 19, 37, 38, 58):
        _p, s = _lib.code(3, prn, 2046, 20)
        assert np.array_equal(s, _NH20), prn


def test_b1i_prn6_known_answer():
    # PRN 6 is a MEO SV. First 10 chips + full-period checksum pinned from the
    # committed b1i_taps.hpp; cross-checked against GNSS-SDR.
    p, _s = _lib.code(3, 6, 2046, 0)
    assert p[:10].tolist() == [1, -1, -1, 1, -1, 1, -1, 1, 1, -1]
    assert int(p.sum()) == 0
    assert hashlib.sha1(p.tobytes()).hexdigest()[:12] == "57c8a31b680b"


def test_b1i_prn1_known_answer():
    p, _s = _lib.code(3, 1, 2046, 0)
    assert p[:10].tolist() == [1, -1, -1, 1, 1, -1, 1, -1, -1, 1]
    assert hashlib.sha1(p.tobytes()).hexdigest()[:12] == "25be6ab9f991"


def test_b1i_prn38_extended_tap_distinct():
    # PRN 38 shares the (2,7) tap pair with PRN 9 but adds the ICD 3.0 third
    # tap (phase3); the two primary codes must differ.
    p9, _ = _lib.code(3, 9, 2046, 0)
    p38, _ = _lib.code(3, 38, 2046, 0)
    assert not np.array_equal(p9, p38)
    assert hashlib.sha1(p38.tobytes()).hexdigest()[:12] == "31952b5b1be3"


def test_b1i_all_prns_balanced_and_binary():
    for prn in range(1, 64):
        p, _ = _lib.code(3, prn, 2046, 0)
        assert p.size == 2046
        assert set(np.unique(p).tolist()) <= {-1, 1}
        assert abs(int(p.sum())) <= 100, prn


def test_b1i_bad_prn_and_short_buffer():
    lib = _lib.load_lib()
    _p = _lib.ctypes.POINTER(_lib.ctypes.c_int8)
    buf = (_lib.ctypes.c_int8 * 2046)()
    assert lib.synth_code(3, 0, buf, 2046, None, 0) == -1
    assert lib.synth_code(3, 64, buf, 2046, None, 0) == -1
    # 1023 <= prim_len < 2046 must be rejected by case 3.
    assert lib.synth_code(3, 6, buf, 1500, None, 0) == -1
    with pytest.raises(ValueError):
        _lib.code(3, 99, 2046, 0)


def test_b1i_abi_still_12():
    assert _lib.load_lib().synth_abi_version() == 13
    assert _lib.ABI_VERSION == 13
