import numpy as np

from backend.synth import _lib


def _carrier(freq, fs, n):
    lib = _lib.load_lib()
    lib.synth_debug_carrier.restype = None
    lib.synth_debug_carrier.argtypes = [_lib.ctypes.c_double, _lib.ctypes.c_double,
                                        _lib.ctypes.c_int,
                                        _lib.ctypes.POINTER(_lib.ctypes.c_float)]
    buf = (_lib.ctypes.c_float * (2 * n))()
    lib.synth_debug_carrier(freq, fs, n, buf)
    a = np.array(list(buf), dtype=np.float32)
    return a[0::2] + 1j * a[1::2]


def test_carrier_frequency_peak_bin():
    fs, n, f = 2_600_000.0, 65536, 123_456.0
    x = _carrier(f, fs, n)
    sp = np.abs(np.fft.fft(x))
    k = int(np.argmax(sp))
    est = k * fs / n
    assert abs(est - f) < fs / n            # within one bin


def test_carrier_unit_amplitude():
    x = _carrier(50_000.0, 2_600_000.0, 4096)
    assert np.allclose(np.abs(x), 1.0, atol=2e-3)   # LUT quantization


def test_carrier_phase_continuity_across_two_blocks():
    fs, n, f = 2_600_000.0, 4096, 77_000.0
    whole = _carrier(f, fs, 2 * n)
    ref = np.exp(2j * np.pi * f * np.arange(2 * n) / fs)
    err = np.angle(whole * np.conj(ref))
    assert np.max(np.abs(np.unwrap(err) - np.mean(err))) < 1e-2
