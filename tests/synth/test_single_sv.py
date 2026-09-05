import numpy as np

from backend import config, inspector
from backend.synth import _lib


def _one_sv(prn, fs, n, code_phase0, code_dopp, carr_freq):
    lib = _lib.load_lib()
    code = (_lib.ctypes.c_int8 * 1023)()
    lib.synth_ca_code(prn, code, 1023)
    lib.synth_debug_one_sv.restype = None
    lib.synth_debug_one_sv.argtypes = [
        _lib.ctypes.POINTER(_lib.ctypes.c_int8),          # code
        _lib.ctypes.c_double, _lib.ctypes.c_double,       # code_rate, code_phase0
        _lib.ctypes.c_double, _lib.ctypes.c_double,       # code_doppler, carrier_freq
        _lib.ctypes.c_double,                             # fs
        _lib.ctypes.c_int,                                # n
        _lib.ctypes.POINTER(_lib.ctypes.c_float)]         # out (2n)
    out = (_lib.ctypes.c_float * (2 * n))()
    lib.synth_debug_one_sv(code, config.CA_CHIP_HZ, code_phase0, code_dopp,
                           carr_freq, fs, n, out)
    a = np.array(list(out), dtype=np.float32)
    return a[0::2] + 1j * a[1::2]


def test_single_sv_acquires_at_expected_doppler_and_phase():
    fs, n, prn = 2_600_000.0, int(2_600_000 * 0.010), 7
    want_dopp, want_phase = 1875.0, 512.0
    x = _one_sv(prn, fs, n, want_phase, want_dopp * config.CA_CHIP_HZ / config.L1_HZ, want_dopp)
    res = inspector.acquire(x, fs, prn, doppler_range=(-6000, 6000), doppler_step=125.0)
    assert abs(res["doppler_hz"] - want_dopp) <= 125.0
    assert min(abs(res["code_phase_chips"] - want_phase),
               1023 - abs(res["code_phase_chips"] - want_phase)) <= 1.0
    assert res["metric_db"] > 12.0
