"""Deterministic RF-impairment layer."""
import numpy as np
import pytest

from backend import impairments
from backend.impairments import ImpairmentConfig, apply

FS = 2.6e6


def _tone(n=20000, f=1000.0):
    t = np.arange(n) / FS
    return np.exp(2j * np.pi * f * t).astype(np.complex64)


def test_default_is_noop():
    x = _tone()
    y, rep = apply(x, FS, ImpairmentConfig())
    assert np.array_equal(x, y)
    assert rep["applied"] == []


def test_from_dict_rejects_conflicting_and_out_of_range():
    with pytest.raises(ValueError):
        ImpairmentConfig.from_dict({"enabled_flag": True, "snr_db": 10, "noise_power": 1.0})
    with pytest.raises(ValueError):
        ImpairmentConfig.from_dict({"clip_fraction": 2.0})
    with pytest.raises(ValueError):
        ImpairmentConfig.from_dict({"quant_bits": 20})


def test_disabled_flag_blocks_everything():
    cfg = ImpairmentConfig(enabled_flag=False, cfo_hz=500.0)
    y, rep = apply(_tone(), FS, cfg)
    assert rep["applied"] == []


def test_cfo_shifts_the_tone():
    x = _tone(f=1000.0)
    cfg = ImpairmentConfig(enabled_flag=True, cfo_hz=300.0)
    y, _ = apply(x, FS, cfg)
    n = len(x)
    freqs = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / FS))
    peak = freqs[np.argmax(np.abs(np.fft.fftshift(np.fft.fft(y))))]
    assert peak == pytest.approx(1300.0, abs=FS / n * 2)


def test_awgn_is_deterministic_and_hits_target_snr():
    x = _tone()
    cfg = ImpairmentConfig(enabled_flag=True, snr_db=10.0, seed=42)
    y1, r1 = apply(x, FS, cfg)
    y2, r2 = apply(x, FS, cfg)
    assert np.array_equal(y1, y2)                       # same seed -> same noise
    assert r1["measured_snr_db"] == pytest.approx(10.0, abs=0.1)
    y3, _ = apply(x, FS, ImpairmentConfig(enabled_flag=True, snr_db=10.0, seed=43))
    assert not np.array_equal(y1, y3)                   # different seed -> different


def test_noise_power_path():
    x = _tone()
    y, rep = apply(x, FS, ImpairmentConfig(enabled_flag=True, noise_power=0.25, seed=1))
    assert rep["noise_power"] == 0.25
    resid = y - x
    assert np.mean(np.abs(resid) ** 2) == pytest.approx(0.25, rel=0.1)


def test_dc_offset_moves_the_mean():
    x = _tone()
    y, _ = apply(x, FS, ImpairmentConfig(enabled_flag=True, dc_i=0.1, dc_q=-0.05))
    assert np.mean(y.real) - np.mean(x.real) == pytest.approx(0.1, abs=0.02)
    assert np.mean(y.imag) - np.mean(x.imag) == pytest.approx(-0.05, abs=0.02)


def test_clip_limits_peak_and_reports_fraction():
    x = _tone() * 3.0
    y, rep = apply(x, FS, ImpairmentConfig(enabled_flag=True, clip_fraction=0.5))
    assert np.max(np.abs(y)) <= 0.5 * np.max(np.abs(x)) + 1e-4
    assert rep["clipped_fraction"] > 0.0


def test_sample_rate_ppm_preserves_length_and_stretches_time():
    x = _tone(f=5000.0)
    y, _ = apply(x, FS, ImpairmentConfig(enabled_flag=True, sample_rate_ppm=100.0))
    assert len(y) == len(x)
    # 100 ppm slower clock -> apparent tone frequency scales by ~(1+1e-4)
    n = len(x)
    freqs = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / FS))
    fx = abs(freqs[np.argmax(np.abs(np.fft.fftshift(np.fft.fft(x))))])
    fy = abs(freqs[np.argmax(np.abs(np.fft.fftshift(np.fft.fft(y))))])
    assert fy >= fx


def test_quantize_reduces_distinct_levels():
    x = _tone() * 100.0
    y, _ = apply(x, FS, ImpairmentConfig(enabled_flag=True, quant_bits=3))
    assert len(np.unique(np.round(y.real, 6))) <= 8


def test_iq_imbalance_creates_correlation():
    x = _tone()
    y, _ = apply(x, FS, ImpairmentConfig(enabled_flag=True, iq_gain_db=1.0, iq_phase_deg=5.0))
    i, q = y.real, y.imag
    corr = np.mean(i * q) / (np.std(i) * np.std(q))
    assert abs(corr) > 0.01


def test_phase_noise_scales_with_setting():
    x = _tone()
    _, r_small = apply(x, FS, ImpairmentConfig(enabled_flag=True, phase_noise_deg_rms=1.0, seed=5))
    _, r_big = apply(x, FS, ImpairmentConfig(enabled_flag=True, phase_noise_deg_rms=10.0, seed=5))
    assert r_big["phase_noise_rad_rms"] > r_small["phase_noise_rad_rms"]
