"""SP-B: the trajectory-knot path in gs::mix_block."""
import numpy as np

from backend import inspector
from backend.synth import _lib

_FS = 2_600_000.0
_KS = 65536                       # knot / block size
_CODE = inspector.ca_code(5).astype(np.int8)
_RATE = 1.023e6
_CARR = 1234.0                    # constant carrier Doppler for the reduction case


def _const_knots(nk, fd, rate, code_phase0):
    """Knot tables that describe a perfectly constant fd / rate, so the
    trajectory path must reproduce the Phase-1 constant-Doppler mixer."""
    eff = _CODE.size - (code_phase0 % _CODE.size)
    cf = [fd] * nk
    cr = [rate] * nk
    cph, kp = [], []
    for j in range(nk):
        tj = (j * _KS) / _FS
        cph.append(2.0 * np.pi * fd * tj)          # accumulated carrier phase
        kp.append(eff + rate * tj)                 # accumulated abs code phase
    return cf, cph, cr, kp


def test_trajectory_reduces_to_phase1_constant_doppler():
    n = 3 * _KS
    nk = 4
    cf, cph, cr, kp = _const_knots(nk, _CARR, _RATE, 0.0)
    traj = _lib.debug_mix_traj(_CODE, _RATE, 0.0, _CARR, _FS, 0, n, _KS,
                               cf, cph, cr, kp)
    ref = _lib.debug_mix_range_ex(_CODE, _RATE, 0.0, 0.0, _CARR, _FS, 0, n)
    assert np.allclose(traj, ref, atol=2e-4)


def test_trajectory_per_block_calls_join_seamlessly():
    # constant knots => three per-block trajectory calls, concatenated, must
    # equal the Phase-1 mixer over the whole span (no glitch at knot seams).
    nk = 4
    cf, cph, cr, kp = _const_knots(nk, 900.0, _RATE + 300.0, 137.0)
    parts = [
        _lib.debug_mix_traj(_CODE, _RATE, 137.0, 900.0, _FS, j * _KS, _KS, _KS,
                            cf, cph, cr, kp)
        for j in range(3)
    ]
    joined = np.concatenate(parts)
    ref = _lib.debug_mix_range_ex(_CODE, _RATE, 137.0, 300.0, 900.0, _FS, 0,
                                  3 * _KS)
    # a seam glitch would be O(0.1+); the residual here is float64 non-
    # associativity between two mathematically identical phase formulas.
    assert np.allclose(joined, ref, atol=2e-3)
    # the seam is at sample _KS: its residual vs the reference is no larger
    # than the residual one sample earlier (no step at the knot boundary).
    d = np.abs(joined - ref)
    assert d[_KS] < 5e-4 and d[2 * _KS] < 5e-4


def test_trajectory_follows_a_carrier_frequency_step():
    # pure tone (all-+1 code) so the FFT peak IS the carrier Doppler.
    ones = np.ones(1023, dtype=np.int8)
    f_hi = 40_000.0
    cf = [0.0, f_hi, f_hi, f_hi]
    cr = [_RATE] * 4
    cph, kp = [], []
    acc = 0.0
    for j in range(4):
        cph.append(acc)
        if j < 3:
            acc += 2.0 * np.pi * cf[j] * (_KS / _FS)
        kp.append(1023 + _RATE * (j * _KS) / _FS)
    blocks = [
        _lib.debug_mix_traj(ones, _RATE, 0.0, 0.0, _FS, j * _KS, _KS, _KS,
                            cf, cph, cr, kp)
        for j in range(3)
    ]

    def peak_hz(seg):
        f = np.fft.fftfreq(len(seg), 1.0 / _FS)
        return f[np.argmax(np.abs(np.fft.fft(seg)))]

    assert abs(peak_hz(blocks[0])) < 200.0
    assert abs(abs(peak_hz(blocks[2])) - f_hi) < 200.0
