import numpy as np
from backend.synth import _lib


def test_boc11_half_rate_of_chip_toggles():
    fs = 2_046_000.0 * 4
    sub = 1_023_000.0                       # BOC(1,1) sub-carrier = 1.023 MHz
    s = _lib.debug_boc(sub, fs, 4096)
    assert set(np.unique(s).tolist()) <= {-1, 1}
    # transitions per second ~ 2*sub
    flips = np.count_nonzero(np.diff(s))
    rate = flips * fs / 4096
    assert abs(rate - 2 * sub) / (2 * sub) < 0.05


def test_boc_phase_continuous_across_two_calls():
    fs, sub = 8_184_000.0, 1_023_000.0
    a = _lib.debug_boc(sub, fs, 1000)
    b = _lib.debug_boc(sub, fs, 2000)       # debug resets phase; compare prefix
    assert np.array_equal(a, b[:1000])
