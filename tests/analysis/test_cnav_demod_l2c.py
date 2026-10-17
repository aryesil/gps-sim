"""CNAV symbols from an L2C capture whose code epoch (and so the symbol
boundary) sits mid-capture-period, not at sample 0."""
import numpy as np
import pytest

from backend.analysis import band_acquire, cnav_decode
from backend.synth import _lib

_FS = 2_600_000.0
_TDM_HZ = 1.023e6
_TDM_LEN = 20460


def _synth(prn, dopp_hz, cm_phase, syms, snr=4.0, seed=3):
    cm, cl = _lib.code_l2c(prn)
    tdm = np.empty(_TDM_LEN)
    tdm[0::2] = cm
    tdm[1::2] = cl[:_TDM_LEN // 2]
    n = np.arange(int(_FS * 0.02 * (len(syms) - 1)))
    u = n / _FS * _TDM_HZ * (1.0 + dopp_hz / 1_227_600_000.0) + 2 * cm_phase
    idx = np.floor(u).astype(np.int64)
    data = syms[idx // _TDM_LEN]                  # symbols start on CM epochs
    sig = tdm[idx % _TDM_LEN] * data * np.exp(2j * np.pi * dopp_hz * n / _FS)
    rng = np.random.default_rng(seed)
    return sig * snr + rng.standard_normal(n.size) + 1j * rng.standard_normal(n.size)


@pytest.mark.parametrize("cm_phase", [2500.0, 5115.0, 8000.0])
def test_l2c_symbols_are_integrated_between_code_epochs(cm_phase):
    rng = np.random.default_rng(7)
    syms = rng.choice([-1.0, 1.0], size=101)
    iq = _synth(9, 900.0, cm_phase, syms)
    a = band_acquire.acquire_l2c(iq, _FS, 9)
    got = cnav_decode.demod_symbols(iq, _FS, 9, dopp_hz=a["doppler_hz"],
                                    code_phase_chips=a["code_phase_chips"])
    # the first CM epoch lies after sample 0: capture symbol 0 is syms[1]
    ref = (syms[1:1 + got.size] < 0).astype(np.int8)
    errs = min(int(np.sum(got != ref)), int(np.sum(got == ref)))
    assert got.size >= 95
    assert errs == 0, (cm_phase, errs)
