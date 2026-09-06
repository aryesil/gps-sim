"""IQ regression: the synthetic generator and the impairment layer are
byte-for-byte reproducible, so a downstream hash/fixture comparison is
stable. Fixtures are built at test time, never committed.
"""
import hashlib

import numpy as np
import pytest

from backend.models import impairments

FS = 2.6e6


def _sha(a: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest()


def test_synth_generator_is_bit_reproducible(synth_iq):
    a, _ = synth_iq(prns=(1, 7, 13, 21), duration_s=0.05, seed=0)
    b, _ = synth_iq(prns=(1, 7, 13, 21), duration_s=0.05, seed=0)
    assert _sha(a) == _sha(b)


def test_synth_changes_with_parameters(synth_iq):
    a, _ = synth_iq(prns=(1, 7), duration_s=0.03)
    b, _ = synth_iq(prns=(1, 8), duration_s=0.03)
    assert _sha(a) != _sha(b)


def test_impairment_chain_is_bit_reproducible(synth_iq):
    inter, _ = synth_iq(prns=(1, 7, 13), duration_s=0.04, quantize=False)
    x = np.asarray(inter).astype(np.complex64)
    cfg = impairments.ImpairmentConfig(
        enabled_flag=True, cfo_hz=250.0, phase_noise_deg_rms=2.0,
        iq_gain_db=0.5, dc_i=0.02, snr_db=8.0, clip_fraction=0.9,
        quant_bits=8, seed=123)
    y1, r1 = impairments.apply(x, FS, cfg)
    y2, r2 = impairments.apply(x, FS, cfg)
    assert _sha(y1) == _sha(y2)
    assert r1["applied"] == r2["applied"]
    assert set(r1["applied"]) == {
        "cfo_hz", "phase_noise_deg_rms", "iq_imbalance", "dc_offset",
        "awgn", "clip", "quantize"}


def test_seed_is_the_only_stochastic_knob(synth_iq):
    inter, _ = synth_iq(prns=(1, 7), duration_s=0.03, quantize=False)
    x = np.asarray(inter).astype(np.complex64)
    base = impairments.ImpairmentConfig(enabled_flag=True, snr_db=5.0, seed=1)
    other = impairments.ImpairmentConfig(enabled_flag=True, snr_db=5.0, seed=2)
    y1, _ = impairments.apply(x, FS, base)
    y2, _ = impairments.apply(x, FS, base)
    y3, _ = impairments.apply(x, FS, other)
    assert _sha(y1) == _sha(y2) and _sha(y1) != _sha(y3)
