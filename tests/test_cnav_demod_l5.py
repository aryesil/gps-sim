"""CNAV symbols from an L5 capture that opens mid-symbol, at an arbitrary
NH10 chip, with the acquisition Doppler several hundred Hz off (a 1 ms
acquisition block straddles an NH10 sign flip). Before the fix only the
satellites whose NH10 period happened to line up with the boundary guess
decoded; the rest returned noise."""
import numpy as np
import pytest

from backend.analysis import cnav_decode
from backend.synth import _lib

_FS = 12_000_000.0
_CHIP_HZ = 10.23e6
_LEN = 10230
_NH10 = np.array([1 if b == "0" else -1 for b in "0000110101"])


def _synth(prn, dopp_hz, start_epoch, chip0, syms, dur_s, snr=0.5, seed=5):
    i5, _q5 = _lib.code_l5(prn)
    n = np.arange(int(_FS * dur_s))
    u = (start_epoch * _LEN + chip0
         + n / _FS * _CHIP_HZ * (1.0 + dopp_hz / 1_176_450_000.0))
    idx = np.floor(u).astype(np.int64)
    ep = idx // _LEN                               # primary epochs so far
    chips = (i5[idx % _LEN] * _NH10[ep % 10] * syms[ep // 20]).astype(np.float32)
    sig = chips * np.exp(2j * np.pi * dopp_hz * n / _FS).astype(np.complex64)
    rng = np.random.default_rng(seed)
    noise = (rng.standard_normal(n.size) + 1j * rng.standard_normal(n.size))
    return (sig * snr + noise).astype(np.complex64)


@pytest.mark.parametrize("start_epoch,dopp_err", [(3, 0.0), (7, 600.0),
                                                   (16, -450.0)])
def test_l5_symbols_with_nh10_and_doppler_offsets(start_epoch, dopp_err):
    prn, dopp, chip0 = 13, 1234.0, 4100.0
    rng = np.random.default_rng(11)
    syms = rng.choice([-1, 1], size=80)
    iq = _synth(prn, dopp, start_epoch, chip0, syms, dur_s=1.3)
    # acquisition convention: circular lag = complement of the start chip
    lag = (_LEN - chip0) % _LEN
    got = cnav_decode.demod_symbols_l5(iq, _FS, prn, dopp_hz=dopp + dopp_err,
                                       code_phase_chips=lag)
    # the capture opens mid-symbol, so its first whole symbol is syms[1]
    # or syms[2]; the carrier-phase sign is ambiguous
    errs = min(int(np.sum(got != ((sh * syms[k:k + got.size]) < 0)))
               for k in (1, 2) for sh in (1, -1))
    assert got.size >= 55
    assert errs == 0, (start_epoch, dopp_err, errs)
