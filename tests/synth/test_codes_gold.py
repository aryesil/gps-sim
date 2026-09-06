import numpy as np

from backend.synth import _lib


def _prim(sys, prn, n):
    p, _ = _lib.code(sys, prn, n)
    return p


def test_gps_code_unchanged_via_new_entry():
    a = _prim(0, 5, 1023)
    b, _ = _lib.code(0, 5, 1023)
    old = np.zeros(1023, np.int8)
    _lib.load_lib().synth_ca_code(
        5, old.ctypes.data_as(_lib.ctypes.POINTER(_lib.ctypes.c_int8)), 1023)
    assert np.array_equal(a, old)
    assert np.array_equal(a, b)


def test_qzss_prn_193_length_and_balance():
    c = _prim(1, 193, 1023)
    assert c.size == 1023
    assert set(np.unique(c).tolist()) <= {-1, 1}
    assert abs(int(c.sum())) <= 65        # near-balanced Gold code


def test_sbas_prn_133_length_and_balance():
    c = _prim(2, 133, 1023)
    assert c.size == 1023
    assert set(np.unique(c).tolist()) <= {-1, 1}
    assert abs(int(c.sum())) <= 65


def test_gps_first_chips_pinned():
    # Locks the GPS Gold path (and thus the shared G2-delay/tap direction) to
    # IS-GPS-200 Table 3-Ia. Chip mapping matches test_ca_code.py: bit 0 -> +1,
    # bit 1 -> -1. PRN 1 first 10 chips (octal 0o1440 = 1100100000).
    assert _prim(0, 1, 1023)[:10].tolist() == [-1, -1, 1, 1, -1, 1, 1, 1, 1, 1]
    # PRN 19 (octal 0o1633 = 1110011011).
    assert _prim(0, 19, 1023)[:10].tolist() == [-1, -1, -1, 1, 1, -1, -1, 1, -1, -1]


def test_qzss_sbas_first_chips_pinned():
    # Pins the committed qzss_sbas_taps.hpp G2-delay tables so an accidental
    # edit or a reversed delay direction cannot pass silently.
    q = _prim(1, 193, 1023)
    assert q[:10].tolist() == [1, 1, 1, 1, -1, 1, 1, 1, -1, 1]
    assert int(q.sum()) == -1
    s = _prim(2, 133, 1023)
    assert s[:10].tolist() == [1, -1, 1, 1, -1, -1, 1, 1, 1, -1]
    assert int(s.sum()) == -1


def test_bad_prn_returns_error():
    rc = _lib.load_lib().synth_code(1, 999,
        (_lib.ctypes.c_int8 * 1023)(), 1023, None, 0)
    assert rc == -1
