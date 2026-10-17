"""Non-frozen band acquisition correlator -- GPS L2C CM code-phase x Doppler."""
import numpy as np
import pytest

from backend.analysis import band_acquire
from backend.synth import _lib

_FS = 5_115_000.0            # 10 samples/chip at 511.5 kcps
_CHIP_HZ = 0.5115e6
_CM_LEN = 10230


def _synth_l2c(prn, dopp_hz, code_phase_chips, nper=6, snr=8.0, seed=1):
    # CM/CL chip-by-chip TDM at 1.023 Mcps, as broadcast (IS-GPS-200 3.3.2.4)
    cm, cl = _lib.code_l2c(prn)
    tdm = np.empty(2 * _CM_LEN, dtype=np.float64)
    tdm[0::2] = cm
    tdm[1::2] = cl[:_CM_LEN]
    npp = int(round(_FS * _CM_LEN / _CHIP_HZ))
    n = np.arange(npp * nper)
    rate = 2 * _CHIP_HZ * (1.0 + dopp_hz / 1_227_600_000.0)
    idx = (np.floor(n / _FS * rate + 2 * code_phase_chips).astype(np.int64)
           ) % (2 * _CM_LEN)
    sig = tdm[idx] * np.exp(2j * np.pi * dopp_hz * n / _FS)
    rng = np.random.default_rng(seed)
    noise = (rng.standard_normal(n.size) + 1j * rng.standard_normal(n.size))
    return sig * snr + noise


def _phase_err(reported, injected):
    # acquire reports the circular lag of the local replica; the synth
    # advances the code by ``injected`` chips, so the two sum to code_len.
    d = (reported + injected) % _CM_LEN
    return min(d, _CM_LEN - d)


def test_acquire_recovers_doppler_and_code_phase():
    iq = _synth_l2c(5, dopp_hz=1200.0, code_phase_chips=3456.0)
    res = band_acquire.acquire_l2c(iq, _FS, 5)
    assert res["metric_db"] > 30.0
    assert abs(res["doppler_hz"] - 1200.0) <= 100.0
    assert _phase_err(res["code_phase_chips"], 3456.0) < 2.0


def test_acquire_l2c_code_phase_is_unbiased_in_cm_chips():
    # the TDM replica must not shift the CM code phase (a continuous CM
    # replica against the TDM signal biases it by ~1/4 CM chip)
    iq = _synth_l2c(7, dopp_hz=0.0, code_phase_chips=1000.25, snr=50.0)
    cp = band_acquire.fine_code_phase(
        iq, _FS, band_acquire.l2c_cm_replica(7),
        chip_hz=band_acquire.L2C_TDM_CHIP_HZ,
        code_len=band_acquire.L2C_TDM_LEN, dopp_hz=0.0) / 2.0
    assert _phase_err(cp, 1000.25) < 0.1          # continuous CM: ~0.4


def test_acquire_l2c_wrapper_matches_prn():
    iq = _synth_l2c(12, dopp_hz=-800.0, code_phase_chips=100.0)
    res = band_acquire.acquire_l2c(iq, _FS, 12)
    assert res["metric_db"] > 30.0
    assert abs(res["doppler_hz"] - (-800.0)) <= 150.0


def test_wrong_prn_does_not_acquire():
    iq = _synth_l2c(5, dopp_hz=0.0, code_phase_chips=0.0)
    right = band_acquire.acquire_l2c(iq, _FS, 5)["metric_db"]
    wrong = band_acquire.acquire_l2c(iq, _FS, 9)["metric_db"]
    assert wrong < right - 15.0


def test_short_capture_returns_sentinel():
    res = band_acquire.acquire(np.zeros(10, np.complex128), _FS,
                               _lib.code_l2c(1)[0].astype(np.float64),
                               chip_hz=_CHIP_HZ, code_len=_CM_LEN)
    assert res["metric_db"] == pytest.approx(-99.0)
