"""Galileo E1-B (data) + E1-C (pilot) memory codes and the E1-C CS25 secondary
(synth_code cases 5 and 6).

E1-B/E1-C are NOT LFSR-generated: they are fixed 4092-chip memory codes from
the Galileo OS SIS ICD (Annex C.7 / C.8), PRN 1..50. The committed hex table
tools/galileo_e1_icd_codes.txt is transcribed verbatim from GNSS-SDR
src/core/system_parameters/Galileo_E1.h (arrays GALILEO_E1_B_PRIMARY_CODE /
GALILEO_E1_C_PRIMARY_CODE; file-commit 2fc172c0f08373271d044d31166811e9316e7649).
tools/gen_galileo_e1.py emits backend/synth/native/galileo_e1_codes.cpp.

CS25 is the ICD GALILEO_E1_C_SECONDARY_CODE "0011100000001010110110010".

Chip map matches the GPS/BeiDou path: code bit 1 -> -1, bit 0 -> +1.
"""
import hashlib

import numpy as np
import pytest

from backend.synth import _lib

E1B_PRN1_FIRST24 = [-1, -1, -1, -1, 1, -1, 1, -1, -1, -1, 1, -1,
                    1, -1, -1, -1, 1, 1, 1, -1, 1, 1, 1, 1]
E1C_PRN1_FIRST24 = [-1, 1, -1, -1, 1, 1, -1, -1, -1, 1, 1, -1,
                    1, 1, -1, -1, 1, -1, 1, 1, 1, 1, 1, 1]
E1B_PRN1_SHA1 = "93f9ef2be37aec8c901569fc94867529a1ed323f"
E1C_PRN1_SHA1 = "ae6eb70da96ec8926428c54d0c6717b94056c58f"
CS25 = np.array([1, 1, -1, -1, -1, 1, 1, 1, 1, 1, 1, 1, -1,
                 1, -1, 1, -1, -1, 1, -1, -1, 1, 1, -1, 1], np.int8)


def test_e1b_e1c_length_and_alphabet():
    for sysc in (5, 6):
        p, s = _lib.code(sysc, 1, 4092, 25 if sysc == 6 else 0)
        assert p.size == 4092
        assert set(np.unique(p).tolist()) <= {-1, 1}
    _, cs = _lib.code(6, 1, 4092, 25)
    assert cs.size == 25


def test_e1b_prn1_known_answer():
    p, _ = _lib.code(5, 1, 4092, 0)
    assert p[:24].tolist() == E1B_PRN1_FIRST24
    assert hashlib.sha1(p.tobytes()).hexdigest() == E1B_PRN1_SHA1


def test_e1c_prn1_known_answer():
    p, _ = _lib.code(6, 1, 4092, 0)
    assert p[:24].tolist() == E1C_PRN1_FIRST24
    assert hashlib.sha1(p.tobytes()).hexdigest() == E1C_PRN1_SHA1


def test_cs25_sequence():
    _, s = _lib.code(6, 1, 4092, 25)
    assert s.size == 25
    assert set(np.unique(s).tolist()) <= {-1, 1}
    assert np.array_equal(s, CS25)


def test_synth_code_6_secondary_is_cs25():
    lib = _lib.load_lib()
    _p = _lib.ctypes.POINTER(_lib.ctypes.c_int8)
    prim = np.zeros(4092, np.int8)
    sec = np.zeros(25, np.int8)
    rc = lib.synth_code(6, 1, prim.ctypes.data_as(_p), 4092,
                        sec.ctypes.data_as(_p), 25)
    assert rc == 0
    assert np.array_equal(sec, CS25)


def test_e1b_no_secondary_written():
    lib = _lib.load_lib()
    _p = _lib.ctypes.POINTER(_lib.ctypes.c_int8)
    prim = np.zeros(4092, np.int8)
    sec = np.full(25, 7, np.int8)
    assert lib.synth_code(5, 1, prim.ctypes.data_as(_p), 4092,
                          sec.ctypes.data_as(_p), 25) == 0
    assert np.array_equal(sec, np.full(25, 7, np.int8))


def test_all_prns_length_and_alphabet():
    for sysc in (5, 6):
        for prn in range(1, 51):
            p, _ = _lib.code(sysc, prn, 4092, 0)
            assert p.size == 4092
            assert set(np.unique(p).tolist()) <= {-1, 1}, (sysc, prn)


def test_bad_prn_and_short_buffer():
    lib = _lib.load_lib()
    _p = _lib.ctypes.POINTER(_lib.ctypes.c_int8)
    buf = (_lib.ctypes.c_int8 * 4092)()
    for sysc in (5, 6):
        assert lib.synth_code(sysc, 0, buf, 4092, None, 0) == -1
        assert lib.synth_code(sysc, 51, buf, 4092, None, 0) == -1
        assert lib.synth_code(sysc, 1, buf, 4091, None, 0) == -1
        with pytest.raises(ValueError):
            _lib.code(sysc, 99, 4092, 0)


def test_abi_still_12():
    assert _lib.load_lib().synth_abi_version() == 13
    assert _lib.ABI_VERSION == 13
