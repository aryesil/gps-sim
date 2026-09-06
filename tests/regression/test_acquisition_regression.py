"""Acquisition regression: the synthetic signal's planted PRNs must be
recoverable, at the planted Doppler and code phase, and impairments must
degrade the acquisition metric in the expected direction -- not silently
break the acquirer.
"""
import numpy as np
import pytest

from backend.models import impairments
from backend import inspector

FS = 2.6e6


def _iq_from(inter):
    return (inter[0::2] + 1j * inter[1::2]).astype(np.complex64)


def test_planted_prns_acquire_with_correct_parameters(synth_iq):
    prns = (1, 7, 13, 21)
    inter, meta = synth_iq(prns=prns, duration_s=0.05)
    iq = _iq_from(inter)
    for p in prns:
        r = inspector.acquire(iq, FS, p, coherent_ms=1, noncoherent=20)
        assert r["metric_db"] > 12.0, (p, r)
        assert r["doppler_hz"] == pytest.approx(meta["dopplers"][p], abs=300.0)
        got = r["code_phase_chips"]
        # acquire() reports the replica shift, which is the negative of the
        # planted code-phase advance, modulo one code period.
        want = (1023.0 - meta["code_phases_chips"][p]) % 1023.0
        err = (got - want + 511.5) % 1023 - 511.5
        assert abs(err) < 1.5, (p, got, want)


def test_absent_prn_does_not_acquire(synth_iq):
    inter, _ = synth_iq(prns=(1, 7, 13, 21), duration_s=0.05, amp=6.0)
    iq = _iq_from(inter)
    # a strong planted PRN clears the 9 dB gate; an absent one does not
    assert inspector.acquire(iq, FS, 1, noncoherent=20)["metric_db"] > 12.0
    r = inspector.acquire(iq, FS, 30, coherent_ms=1, noncoherent=20)
    assert r["metric_db"] < 9.0


def test_awgn_lowers_the_metric_but_keeps_strong_prn(synth_iq):
    inter, meta = synth_iq(prns=(1, 7, 13), duration_s=0.06, amp=20.0)
    clean = _iq_from(inter).astype(np.complex64)
    base = inspector.acquire(clean, FS, 1, noncoherent=30)["metric_db"]
    noisy, _ = impairments.apply(clean, FS, impairments.ImpairmentConfig(
        enabled_flag=True, snr_db=-15.0, seed=7))
    deg = inspector.acquire(noisy, FS, 1, noncoherent=30)["metric_db"]
    assert deg < base
    assert deg > 6.0                       # still detectable at this C/N0


def test_cfo_shows_up_in_the_doppler_estimate(synth_iq):
    inter, meta = synth_iq(prns=(7,), dopplers={7: 0.0}, duration_s=0.05)
    iq = _iq_from(inter).astype(np.complex64)
    shifted, _ = impairments.apply(iq, FS, impairments.ImpairmentConfig(
        enabled_flag=True, cfo_hz=1200.0))
    r = inspector.acquire(shifted, FS, 7, noncoherent=20)
    assert r["doppler_hz"] == pytest.approx(1200.0, abs=350.0)
