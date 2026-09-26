"""Streaming band-file post-processing must match the in-memory models."""
import math

import numpy as np
import pytest

from backend import generator
from backend.models import impairments as imp
from backend.models import multipath as mp_mod
from backend.synth import postproc

_FS = 2_600_000.0


@pytest.fixture(autouse=True)
def _small_chunks(monkeypatch):
    # several chunks per file, so every chunk seam is exercised
    monkeypatch.setattr(postproc, "_CHUNK", 3001)


def _write(tmp_path, n=20000, seed=1, quant=2, amp=3000.0):
    rng = np.random.default_rng(seed)
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * amp
    dtype = np.int8 if quant == 0 else np.int16
    fsc = postproc._FULL_SCALE[quant]
    inter = np.empty(2 * n, dtype=dtype)
    inter[0::2] = np.clip(np.round(x.real), -fsc, fsc)
    inter[1::2] = np.clip(np.round(x.imag), -fsc, fsc)
    p = tmp_path / "band.bin"
    inter.tofile(p)
    return p, (inter[0::2].astype(np.float32) + 1j * inter[1::2]).astype(np.complex64)


def _load(p, quant=2):
    raw = np.fromfile(p, dtype=np.int8 if quant == 0 else np.int16)
    return raw[0::2].astype(np.float32) + 1j * raw[1::2]


@pytest.mark.parametrize("kw", [
    {"cfo_hz": 1234.5},
    {"iq_gain_db": 1.5, "iq_phase_deg": 7.0},
    {"dc_i": 0.05, "dc_q": -0.02},
    {"clip_fraction": 0.4},
    {"quant_bits": 4},
    {"sample_rate_ppm": 40.0},
    {"cfo_hz": 300.0, "iq_gain_db": 1.0, "dc_i": 0.03, "clip_fraction": 0.6,
     "quant_bits": 6},
])
def test_deterministic_stages_match_in_memory(tmp_path, kw):
    p, x = _write(tmp_path)
    cfg = imp.ImpairmentConfig(enabled_flag=True, **kw)
    want, _ = imp.apply(x, _FS, cfg)
    want = np.clip(np.round(want.real), -32767, 32767) + 1j * np.clip(
        np.round(want.imag), -32767, 32767)
    rep = postproc.apply_impairments(p, _FS, 2, cfg)
    got = _load(p)
    assert got.size == x.size
    assert np.max(np.abs(got - want)) <= 1.5, kw
    assert rep["clean_output"] is None


def test_awgn_hits_requested_snr_and_is_reproducible(tmp_path):
    cfg = imp.ImpairmentConfig(enabled_flag=True, snr_db=3.0, seed=9)
    p, x = _write(tmp_path)
    rep = postproc.apply_impairments(p, _FS, 2, cfg)
    a = _load(p)
    noise = a - x
    snr = 10 * math.log10(np.mean(np.abs(x) ** 2) / np.mean(np.abs(noise) ** 2))
    assert abs(snr - 3.0) < 0.2
    assert abs(rep["measured_snr_db"] - 3.0) < 1e-6
    p2, _ = _write(tmp_path)                     # same input again
    postproc.apply_impairments(p2, _FS, 2, cfg)
    assert np.array_equal(_load(p2), a)
    p3, _ = _write(tmp_path)
    postproc.apply_impairments(p3, _FS, 2, cfg, band_idx=1)
    assert not np.array_equal(_load(p3), a)     # bands get their own noise


def test_phase_noise_is_one_continuous_walk_across_chunks(tmp_path):
    p, x = _write(tmp_path, amp=1000.0)
    cfg = imp.ImpairmentConfig(enabled_flag=True, phase_noise_deg_rms=2000.0,
                               seed=4)
    postproc.apply_impairments(p, _FS, 2, cfg)
    ph = np.angle(_load(p) * np.conj(x))
    step = np.abs(np.angle(np.exp(1j * np.diff(ph))))
    # a restarted walk would jump at a 3001-sample seam
    seams = step[3000::3001]
    assert seams.max() < 10 * np.median(step) + 0.05


@pytest.mark.parametrize("quant", [0, 2])
def test_multipath_matches_in_memory_channel(tmp_path, quant):
    amp = 20.0 if quant == 0 else 3000.0
    p, x = _write(tmp_path, quant=quant, amp=amp)
    cfg = mp_mod.MultipathConfig.from_dict({"model": "specular", "reflections": [
        {"excess_delay_m": 150.0, "amplitude": 0.5, "phase_rad": 1.0},
        {"excess_delay_m": 400.0, "amplitude": 0.3}]})
    rep = postproc.apply_multipath(p, _FS, quant, cfg)
    acc = np.zeros(x.size, dtype=np.complex64)
    for d_s, g in mp_mod.channel_taps(cfg, 0.0):
        acc += g * generator._frac_delay(x, d_s * _FS)
    k = math.sqrt(np.sum(np.abs(x) ** 2) / np.sum(np.abs(acc) ** 2))
    got = _load(p, quant)
    assert rep["level_rescaled_by"] == pytest.approx(k, rel=1e-4)
    assert np.max(np.abs(got - acc * k)) <= 1.5


def test_multipath_reflection_doppler_rotates_per_sample(tmp_path):
    n = 26000
    p = tmp_path / "band.bin"
    inter = np.zeros(2 * n, dtype=np.int16)
    inter[0::2] = 1000
    inter.tofile(p)                              # constant DC input
    cfg = mp_mod.MultipathConfig.from_dict({"model": "specular", "reflections": [
        {"excess_delay_m": 0.0, "amplitude": 0.5, "phase_rad": 0.0,
         "doppler_hz": 100.0}]})
    postproc.apply_multipath(p, _FS, 2, cfg)
    y = _load(p)
    t = np.arange(n) / _FS
    shape = np.abs(1 + 0.5 * np.exp(2j * np.pi * 100.0 * t))
    assert np.corrcoef(np.abs(y), shape)[0, 1] > 0.99
