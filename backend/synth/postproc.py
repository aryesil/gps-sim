"""Streaming IQ post-processing for native-engine band files.

The gps-sdr-sim path (``backend.generator``) post-processes its one small
L1 file in memory. Native band files can be many GB (L5 at 25 Msps for
30 s is 750 M samples), so this module applies the same models chunk by
chunk from a memory map and writes the result back through a temporary
file. Nothing here holds more than a few chunks in memory.

* ``apply_multipath`` -- the specular-multipath channel of
  ``backend.models.multipath``: direct ray plus delayed, scaled, rotated
  copies of the composite signal. Unlike the gps-sdr-sim path, each
  reflection's Doppler rotates its tap sample by sample instead of being
  frozen at mid-scenario.
* ``apply_impairments`` -- the RF impairments of
  ``backend.models.impairments``, in the same fixed order. Stages that
  normalise to a whole-signal statistic (DC and clip to the peak, AWGN to
  the mean power, requantisation to the peak) get it from an extra
  read-only pass. Randomness is keyed on (seed, band, stage, chunk), so a
  pass can regenerate exactly the noise another pass saw.

No clean copy is kept: generation is deterministic, so the same request
without the model reproduces the clean file.
"""
from __future__ import annotations

import math
import os
import pathlib

import numpy as np

from backend.models import impairments as imp
from backend.models import multipath as mp_mod

_CHUNK = 1 << 21                       # complex samples per work chunk
_FULL_SCALE = {0: 127.0, 1: 2047.0, 2: 32767.0}
_DTYPE = {0: np.int8, 1: np.int16, 2: np.int16}
_STAGE_PN, _STAGE_AWGN = 1, 2


def _open(path: pathlib.Path, quant: int) -> np.memmap:
    raw = np.memmap(path, dtype=_DTYPE[quant], mode="r")
    return raw[: raw.size - raw.size % 2]


def _read(raw: np.memmap, a: int, b: int) -> np.ndarray:
    """Complex samples [a, b) of an interleaved-IQ memmap; zero outside."""
    n = raw.size // 2
    out = np.zeros(b - a, dtype=np.complex64)
    lo, hi = max(a, 0), min(b, n)
    if hi > lo:
        seg = raw[2 * lo:2 * hi]
        out[lo - a:hi - a].real = seg[0::2]
        out[lo - a:hi - a].imag = seg[1::2]
    return out


class _Writer:
    """Writes interleaved IQ to ``<path>.tmp`` and swaps it in on close."""

    def __init__(self, path: pathlib.Path, quant: int):
        self.path, self.quant = path, quant
        self.tmp = path.with_name(path.name + ".tmp")
        self.f = open(self.tmp, "wb")
        self.fs = _FULL_SCALE[quant]

    def write(self, x: np.ndarray) -> None:
        inter = np.empty(2 * x.size, dtype=_DTYPE[self.quant])
        inter[0::2] = np.clip(np.round(x.real), -self.fs, self.fs)
        inter[1::2] = np.clip(np.round(x.imag), -self.fs, self.fs)
        inter.tofile(self.f)

    def close(self) -> None:
        self.f.close()
        os.replace(self.tmp, self.path)


# --- multipath ---------------------------------------------------------------

def _mp_chunk(raw, a, b, fs, taps):
    """Channel output for samples [a, b). ``taps`` = [(shift_samples,
    amplitude, phase_rad, doppler_hz)], direct ray first."""
    pad = int(math.ceil(max(t[0] for t in taps))) + 2
    x = _read(raw, a - pad, b + 1)     # +1: right neighbour of the last sample
    t = (np.arange(a, b, dtype=np.float64)) / fs
    out = np.zeros(b - a, dtype=np.complex64)
    for shift, amp, ph, fd in taps:
        src = np.arange(a, b, dtype=np.float64) - shift - (a - pad)
        i = np.floor(src).astype(np.int64)
        f = (src - i).astype(np.float32)
        y = (1.0 - f) * x[i] + f * x[i + 1]
        if (a - pad) < 0:                       # before the file start: zero
            y[(np.arange(a, b) - shift) < 0] = 0.0
        g = amp * np.exp(1j * (ph + 2.0 * np.pi * fd * t))
        out += (g * y).astype(np.complex64)
    return out


def apply_multipath(path: pathlib.Path, fs: float, quant: int,
                    cfg: mp_mod.MultipathConfig) -> dict | None:
    """Convolve the band file with the specular-multipath channel in place.
    Level is rescaled to the input RMS, as on the gps-sdr-sim path."""
    if not cfg.enabled:
        return None
    C = 299792458.0
    taps = [(0.0, 1.0, 0.0, 0.0)] + [
        (r.excess_delay_m / C * fs, r.amplitude, r.phase_rad, r.doppler_hz)
        for r in cfg.reflections]
    raw = _open(path, quant)
    n = raw.size // 2
    p_in = p_out = 0.0
    for a in range(0, n, _CHUNK):
        b = min(n, a + _CHUNK)
        p_in += float(np.sum(np.abs(_read(raw, a, b)) ** 2))
        p_out += float(np.sum(np.abs(_mp_chunk(raw, a, b, fs, taps)) ** 2))
    k = math.sqrt(p_in / p_out) if p_out > 0 else 1.0
    w = _Writer(path, quant)
    try:
        for a in range(0, n, _CHUNK):
            b = min(n, a + _CHUNK)
            w.write(_mp_chunk(raw, a, b, fs, taps) * np.float32(k))
    finally:
        del raw
        w.close()
    return {"model": cfg.model, "n_reflections": len(cfg.reflections),
            "level_rescaled_by": k, "doppler": "per-sample"}


# --- impairments -------------------------------------------------------------

def _rng(seed, band_idx, stage, chunk):
    return np.random.default_rng([int(seed) & 0xFFFFFFFF, band_idx, stage, chunk])


class _Pipe:
    """One pass of the impairment chain over the file, stopping after
    ``upto`` (a stage name) so earlier passes can gather statistics."""

    def __init__(self, raw, fs, cfg, band_idx, stats):
        self.raw, self.fs, self.cfg, self.bi, self.st = raw, fs, cfg, band_idx, stats
        self.n = raw.size // 2

    def run(self, upto, sink):
        cfg, n, fs = self.cfg, self.n, self.fs
        ph_carry = 0.0
        scale = 1.0 + cfg.sample_rate_ppm * 1e-6
        for ci, a in enumerate(range(0, n, _CHUNK)):
            b = min(n, a + _CHUNK)
            idx = np.arange(a, b, dtype=np.float64)
            if cfg.sample_rate_ppm:
                src = np.clip(idx * scale, 0.0, n - 1.0)
                lo = int(np.floor(src[0]))
                hi = int(np.floor(src[-1])) + 2
                x = _read(self.raw, lo, hi)
                i = np.floor(src).astype(np.int64)
                f = (src - i).astype(np.float32)
                j = np.minimum(i + 1, n - 1)
                x = ((1.0 - f) * x[i - lo] + f * x[j - lo]).astype(np.complex64)
            else:
                x = _read(self.raw, a, b)
            if cfg.cfo_hz:
                x = x * np.exp(2j * np.pi * cfg.cfo_hz * idx / fs).astype(np.complex64)
            if cfg.phase_noise_deg_rms:
                step = math.radians(cfg.phase_noise_deg_rms) / math.sqrt(max(fs, 1.0))
                ph = ph_carry + np.cumsum(
                    _rng(cfg.seed, self.bi, _STAGE_PN, ci).normal(0.0, step, b - a))
                ph_carry = float(ph[-1])
                x = x * np.exp(1j * ph).astype(np.complex64)
                if upto == "out":
                    self.st["ph_s1"] += float(np.sum(ph))
                    self.st["ph_s2"] += float(np.sum(ph * ph))
            if cfg.iq_gain_db or cfg.iq_phase_deg:
                g = 10.0 ** (cfg.iq_gain_db / 20.0)
                eps = math.radians(cfg.iq_phase_deg)
                q = x.imag * g
                q = q * math.cos(eps) + x.real * math.sin(eps)
                x = (x.real + 1j * q).astype(np.complex64)
            if upto == "pre_dc":
                sink(x)
                continue
            if cfg.dc_i or cfg.dc_q:
                pk = self.st["peak_pre_dc"] or 1.0
                x = x + np.complex64(complex(cfg.dc_i * pk, cfg.dc_q * pk))
            if cfg.snr_db is not None or cfg.noise_power is not None:
                sigma = math.sqrt(self.st["noise_power"] / 2.0)
                r = _rng(cfg.seed, self.bi, _STAGE_AWGN, ci)
                x = (x + r.normal(0.0, sigma, b - a)
                     + 1j * r.normal(0.0, sigma, b - a)).astype(np.complex64)
            if upto == "pre_clip":
                sink(x)
                continue
            if cfg.clip_fraction:
                lim = cfg.clip_fraction * (self.st["peak_pre_clip"] or 1.0)
                mag = np.abs(x)
                over = mag > lim
                if over.any():
                    x = x.copy()
                    x[over] = (x[over] / mag[over] * lim).astype(np.complex64)
                if upto == "out":
                    self.st["n_clipped"] += int(np.sum(over))
            if upto == "pre_quant":
                sink(x)
                continue
            if cfg.quant_bits:
                pk = self.st["peak_pre_quant"] or 1.0
                lv = 2 ** cfg.quant_bits / 2 - 1
                x = ((np.round(x.real / pk * lv) + 1j * np.round(x.imag / pk * lv))
                     / lv * pk).astype(np.complex64)
            sink(x)


def apply_impairments(path: pathlib.Path, fs: float, quant: int,
                      cfg: imp.ImpairmentConfig, band_idx: int = 0) -> dict | None:
    """Impair the band file in place. Report keys match
    ``impairments.apply``."""
    if not cfg.enabled:
        return None
    raw = _open(path, quant)
    n = raw.size // 2
    st = {"ph_s1": 0.0, "ph_s2": 0.0, "n_clipped": 0,
          "peak_pre_dc": 0.0, "peak_pre_clip": 0.0, "peak_pre_quant": 0.0,
          "noise_power": 0.0}
    pipe = _Pipe(raw, fs, cfg, band_idx, st)
    awgn = cfg.snr_db is not None or cfg.noise_power is not None

    if cfg.dc_i or cfg.dc_q or awgn:
        acc = {"pk": 0.0, "s": 0j, "p": 0.0}

        def sink1(x):
            if x.size:
                acc["pk"] = max(acc["pk"], float(np.max(np.abs(x))))
                acc["s"] += complex(np.sum(x, dtype=np.complex128))
                acc["p"] += float(np.sum(np.abs(x) ** 2))
        pipe.run("pre_dc", sink1)
        st["peak_pre_dc"] = acc["pk"]
        pk = acc["pk"] or 1.0
        dc = complex(cfg.dc_i * pk, cfg.dc_q * pk)
        mean = acc["s"] / max(n, 1)
        sig_p = acc["p"] / max(n, 1) + 2.0 * (dc.conjugate() * mean).real + abs(dc) ** 2
        sig_p = sig_p or 1.0
        st["sig_power"] = sig_p
        if cfg.noise_power is not None:
            st["noise_power"] = float(cfg.noise_power)
        elif cfg.snr_db is not None:
            st["noise_power"] = sig_p / (10.0 ** (cfg.snr_db / 10.0))

    if cfg.clip_fraction:
        acc2 = {"pk": 0.0}

        def sink2(x):
            if x.size:
                acc2["pk"] = max(acc2["pk"], float(np.max(np.abs(x))))
        pipe.run("pre_clip", sink2)
        st["peak_pre_clip"] = acc2["pk"]

    if cfg.quant_bits:
        acc3 = {"pk": 0.0}

        def sink3(x):
            if x.size:
                acc3["pk"] = max(acc3["pk"], float(np.max(np.abs(x.real))),
                                 float(np.max(np.abs(x.imag))))
        pipe.run("pre_quant", sink3)
        st["peak_pre_quant"] = acc3["pk"]

    w = _Writer(path, quant)
    try:
        pipe.run("out", w.write)
    finally:
        del raw, pipe
        w.close()

    applied = [name for flag, name in (
        (cfg.sample_rate_ppm, "sample_rate_ppm"), (cfg.cfo_hz, "cfo_hz"),
        (cfg.phase_noise_deg_rms, "phase_noise_deg_rms"),
        (cfg.iq_gain_db or cfg.iq_phase_deg, "iq_imbalance"),
        (cfg.dc_i or cfg.dc_q, "dc_offset"), (awgn, "awgn"),
        (cfg.clip_fraction, "clip"), (cfg.quant_bits, "quantize")) if flag]
    rep = {"applied": applied, "seed": cfg.seed, "n_samples": int(n),
           "band_index": band_idx, "clean_output": None}
    if cfg.phase_noise_deg_rms and n:
        m1 = st["ph_s1"] / n
        rep["phase_noise_rad_rms"] = math.sqrt(max(st["ph_s2"] / n - m1 * m1, 0.0))
    if awgn:
        rep["noise_power"] = st["noise_power"]
        rep["measured_snr_db"] = float(10.0 * math.log10(
            st["sig_power"] / st["noise_power"])) if st["noise_power"] else None
    if cfg.clip_fraction:
        rep["clipped_fraction"] = st["n_clipped"] / max(n, 1)
    return rep
