import numpy as np

from backend.synth import _lib


def _nav(mode, bits, fs, n):
    lib = _lib.load_lib()
    lib.synth_debug_nav.restype = None
    lib.synth_debug_nav.argtypes = [_lib.ctypes.c_int,
                                    _lib.ctypes.POINTER(_lib.ctypes.c_int8),
                                    _lib.ctypes.c_int, _lib.ctypes.c_double,
                                    _lib.ctypes.c_int,
                                    _lib.ctypes.POINTER(_lib.ctypes.c_int8)]
    barr = (_lib.ctypes.c_int8 * max(1, len(bits)))(*bits)
    out = (_lib.ctypes.c_int8 * n)()
    lib.synth_debug_nav(mode, barr, len(bits), fs, n, out)
    return np.array(list(out))


def test_zero_mode_is_constant_plus_one():
    assert np.all(_nav(0, [], 50_000.0, 5000) == 1)


def test_known_frame_repeats_at_50_hz():
    fs = 50_000.0                       # 1000 samples per nav bit
    bits = [1, -1, -1, 1]
    out = _nav(1, bits, fs, 4000)
    assert np.all(out[0:1000] == 1)
    assert np.all(out[1000:2000] == -1)
    assert np.all(out[3000:4000] == 1)
