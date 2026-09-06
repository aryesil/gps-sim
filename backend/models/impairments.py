"""Deterministic RF-impairment layer for generated IQ.

Disabled by default. Every impairment is independently switchable and
fully deterministic: all randomness is drawn from a single
``numpy.random.default_rng(seed)`` so a given (config, seed, input)
always yields the same output. Nothing here runs unless a caller opts
in; the clean gps-sdr-sim output is untouched otherwise.

The layer operates on a complex baseband array at a known sample rate
and returns a new array plus a report of what was applied. Order is
fixed and documented in ``apply``.

Impairments
-----------
* ``cfo_hz``            -- carrier frequency offset (LO error)
* ``sample_rate_ppm``   -- sampling-clock rate error (time-base stretch)
* ``phase_noise_deg_rms`` -- integrated-phase-noise random walk
* ``iq_gain_db`` / ``iq_phase_deg`` -- I/Q amplitude & quadrature imbalance
* ``dc_i`` / ``dc_q``   -- DC offset on each rail (fraction of full scale)
* ``snr_db`` or ``noise_power``  -- additive white Gaussian noise
* ``clip_fraction``     -- hard clip at this fraction of the peak magnitude
* ``quant_bits``        -- uniform requantisation to this many bits
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass
class ImpairmentConfig:
    enabled_flag: bool = False
    seed: int = 0
    cfo_hz: float = 0.0
    sample_rate_ppm: float = 0.0
    phase_noise_deg_rms: float = 0.0
    iq_gain_db: float = 0.0
    iq_phase_deg: float = 0.0
    dc_i: float = 0.0
    dc_q: float = 0.0
    snr_db: float | None = None
    noise_power: float | None = None
    clip_fraction: float = 0.0
    quant_bits: int = 0

    @property
    def enabled(self) -> bool:
        return self.enabled_flag and any((
            self.cfo_hz, self.sample_rate_ppm, self.phase_noise_deg_rms,
            self.iq_gain_db, self.iq_phase_deg, self.dc_i, self.dc_q,
            self.snr_db is not None, self.noise_power is not None,
            self.clip_fraction, self.quant_bits))

    @classmethod
    def from_dict(cls, d: dict | None) -> "ImpairmentConfig":
        if not d:
            return cls()
        keys = {f for f in cls.__dataclass_fields__}
        cfg = cls(**{k: d[k] for k in d if k in keys})
        if cfg.snr_db is not None and cfg.noise_power is not None:
            raise ValueError("impairments: give snr_db or noise_power, not both")
        if not (0.0 <= cfg.clip_fraction <= 1.0):
            raise ValueError("impairments: clip_fraction must be in [0, 1]")
        if cfg.quant_bits < 0 or cfg.quant_bits > 16:
            raise ValueError("impairments: quant_bits must be in [0, 16]")
        return cfg


def _resample_ppm(iq: np.ndarray, ppm: float) -> np.ndarray:
    """Linear resample onto a clock stretched by ``ppm`` parts per million.
    Output length matches the input so downstream sizing is unchanged."""
    n = len(iq)
    scale = 1.0 + ppm * 1e-6
    src = np.arange(n) * scale
    src = np.clip(src, 0.0, n - 1.0)
    lo = np.floor(src).astype(np.int64)
    hi = np.minimum(lo + 1, n - 1)
    frac = (src - lo).astype(np.float64)
    return ((1.0 - frac) * iq[lo] + frac * iq[hi]).astype(np.complex64)


def apply(iq: np.ndarray, sample_rate: float,
          cfg: ImpairmentConfig) -> tuple[np.ndarray, dict]:
    """Return ``(impaired_iq, report)``. ``iq`` is complex baseband.

    Fixed order: sample-rate error, CFO, phase noise, I/Q imbalance,
    DC offset, AWGN, clipping, quantisation.
    """
    x = np.asarray(iq).astype(np.complex64)
    report: dict = {"applied": [], "seed": cfg.seed, "n_samples": int(len(x))}
    if not cfg.enabled or len(x) == 0:
        report["applied"] = []
        return x, report

    rng = np.random.default_rng(cfg.seed)
    n = len(x)
    t = np.arange(n) / sample_rate

    if cfg.sample_rate_ppm:
        x = _resample_ppm(x, cfg.sample_rate_ppm)
        report["applied"].append("sample_rate_ppm")

    if cfg.cfo_hz:
        x = x * np.exp(2j * np.pi * cfg.cfo_hz * t).astype(np.complex64)
        report["applied"].append("cfo_hz")

    if cfg.phase_noise_deg_rms:
        # random-walk phase: cumulative Gaussian increments scaled so the
        # per-sample step std matches the requested integrated RMS over 1 s
        step = math.radians(cfg.phase_noise_deg_rms) / math.sqrt(max(sample_rate, 1.0))
        ph = np.cumsum(rng.normal(0.0, step, n))
        x = x * np.exp(1j * ph).astype(np.complex64)
        report["applied"].append("phase_noise_deg_rms")
        report["phase_noise_rad_rms"] = float(np.std(ph))

    if cfg.iq_gain_db or cfg.iq_phase_deg:
        g = 10.0 ** (cfg.iq_gain_db / 20.0)
        eps = math.radians(cfg.iq_phase_deg)
        i = x.real
        q = x.imag * g
        # skew Q by eps relative to I
        q = q * math.cos(eps) + i * math.sin(eps)
        x = (i + 1j * q).astype(np.complex64)
        report["applied"].append("iq_imbalance")

    if cfg.dc_i or cfg.dc_q:
        peak = float(np.max(np.abs(x))) or 1.0
        x = x + np.complex64(complex(cfg.dc_i * peak, cfg.dc_q * peak))
        report["applied"].append("dc_offset")

    if cfg.snr_db is not None or cfg.noise_power is not None:
        sig_p = float(np.mean(np.abs(x) ** 2)) or 1.0
        if cfg.noise_power is not None:
            npow = float(cfg.noise_power)
        else:
            npow = sig_p / (10.0 ** (cfg.snr_db / 10.0))
        sigma = math.sqrt(npow / 2.0)
        noise = rng.normal(0.0, sigma, n) + 1j * rng.normal(0.0, sigma, n)
        x = (x + noise).astype(np.complex64)
        report["applied"].append("awgn")
        report["noise_power"] = npow
        report["measured_snr_db"] = float(10.0 * math.log10(sig_p / npow))

    if cfg.clip_fraction:
        peak = float(np.max(np.abs(x))) or 1.0
        lim = cfg.clip_fraction * peak
        mag = np.abs(x)
        over = mag > lim
        if over.any():
            x = x.copy()
            x[over] = (x[over] / mag[over] * lim).astype(np.complex64)
        report["applied"].append("clip")
        report["clipped_fraction"] = float(np.mean(over))

    if cfg.quant_bits:
        peak = float(np.max(np.abs(np.concatenate([x.real, x.imag])))) or 1.0
        levels = 2 ** cfg.quant_bits
        q = np.round(x.real / peak * (levels / 2 - 1)) / (levels / 2 - 1) * peak
        qi = np.round(x.imag / peak * (levels / 2 - 1)) / (levels / 2 - 1) * peak
        x = (q + 1j * qi).astype(np.complex64)
        report["applied"].append("quantize")

    return x, report
