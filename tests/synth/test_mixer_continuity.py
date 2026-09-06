"""Regression coverage for B1 (final review): the code NCO in gs::mix_block must
advance with the *absolute* sample index, so that a span synthesized in one call
is identical to the same span synthesized as disjoint sub-ranges -- and identical
regardless of how mix_block_parallel splits it across threads.

Drives the compiled kernel directly through the synth_debug_mix_range /
synth_debug_mix_parallel shims (mix_block / mix_block_parallel are not in the
extern "C" ABI otherwise).
"""
import numpy as np

from backend import config
from backend.synth import _lib

FS = 2_600_000.0
PRN = 7
CODE_DOPP_HZ = 5.0  # nonzero, so the dropped absolute term actually bites
CARR_HZ = 1234.0


def _code(prn):
    lib = _lib.load_lib()
    buf = (_lib.ctypes.c_int8 * 1023)()
    lib.synth_ca_code(prn, buf, 1023)
    return buf


def _mix_range(code, sample0, n):
    lib = _lib.load_lib()
    lib.synth_debug_mix_range.restype = None
    lib.synth_debug_mix_range.argtypes = [
        _lib.ctypes.POINTER(_lib.ctypes.c_int8),
        _lib.ctypes.c_double, _lib.ctypes.c_double,      # code_rate, code_phase0
        _lib.ctypes.c_double, _lib.ctypes.c_double,      # code_doppler, carrier
        _lib.ctypes.c_double,                            # fs
        _lib.ctypes.c_uint64, _lib.ctypes.c_int,         # sample0, n
        _lib.ctypes.POINTER(_lib.ctypes.c_float)]
    out = (_lib.ctypes.c_float * (2 * n))()
    lib.synth_debug_mix_range(code, config.CA_CHIP_HZ, 512.0, CODE_DOPP_HZ,
                              CARR_HZ, FS, sample0, n, out)
    a = np.array(list(out), dtype=np.float32)
    return a[0::2] + 1j * a[1::2]


def _mix_parallel(code, n, nthreads):
    lib = _lib.load_lib()
    lib.synth_debug_mix_parallel.restype = None
    lib.synth_debug_mix_parallel.argtypes = [
        _lib.ctypes.POINTER(_lib.ctypes.c_int8),
        _lib.ctypes.c_double, _lib.ctypes.c_double,
        _lib.ctypes.c_double, _lib.ctypes.c_double,
        _lib.ctypes.c_double,
        _lib.ctypes.c_uint64, _lib.ctypes.c_int, _lib.ctypes.c_int,
        _lib.ctypes.POINTER(_lib.ctypes.c_float)]
    out = (_lib.ctypes.c_float * (2 * n))()
    lib.synth_debug_mix_parallel(code, config.CA_CHIP_HZ, 512.0, CODE_DOPP_HZ,
                                 CARR_HZ, FS, 0, n, nthreads, out)
    a = np.array(list(out), dtype=np.float32)
    return a[0::2] + 1j * a[1::2]


def test_split_range_matches_single_shot():
    code = _code(PRN)
    n, m = 100_000, 40_000
    single = _mix_range(code, 0, n)
    split = np.concatenate([_mix_range(code, 0, m), _mix_range(code, m, n - m)])
    assert np.max(np.abs(single - split)) < 1e-2


def test_thread_count_invariance():
    code = _code(PRN)
    n = 300_000
    iq1 = _mix_parallel(code, n, 1)
    iq4 = _mix_parallel(code, n, 4)
    assert np.max(np.abs(iq1 - iq4)) < 1e-2
