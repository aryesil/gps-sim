"""Structural and statistical integrity checks for a generated IQ file.

Pure inspection -- reads a gps-sdr-sim ``.bin`` (interleaved int8/int16,
I,Q,I,Q...) and reports whether it is well-formed and physically
plausible. No hardware, no receiver model. ``validate_file`` returns a
report with a top-level ``ok`` flag and a list of ``problems``; callers
decide what is fatal.

Checks
------
* file size is a whole number of complex samples and matches the
  expected duration x sample rate (when given)
* no NaN / Inf (int formats cannot carry them, but a converted float
  buffer can -- checked when an array is passed directly)
* numeric range within the format's full scale; fraction of samples at
  full scale (clipping)
* DC offset (mean I, mean Q) small relative to RMS
* I/Q power balance and correlation (quadrature quality)
* occupied bandwidth from the power spectrum vs the sample rate
"""
from __future__ import annotations

import numpy as np

from backend import inspector

_FULL_SCALE = {"int8": 127.0, "int16": 32767.0}


def _occupied_bandwidth(iq: np.ndarray, sample_rate: float, frac: float = 0.99):
    n = min(len(iq), 65536)
    if n < 16:
        return 0.0
    seg = iq[:n] * np.hanning(n)
    psd = np.abs(np.fft.fftshift(np.fft.fft(seg))) ** 2
    freqs = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / sample_rate))
    total = psd.sum()
    if total <= 0:
        return 0.0
    csum = np.cumsum(psd) / total
    lo = np.searchsorted(csum, (1.0 - frac) / 2.0)
    hi = np.searchsorted(csum, 1.0 - (1.0 - frac) / 2.0)
    return float(freqs[min(hi, n - 1)] - freqs[lo])


def validate_array(iq: np.ndarray, sample_rate: float, *,
                   sample_format: str = "int16",
                   expected_duration_s: float | None = None) -> dict:
    problems: list[str] = []
    iq = np.asarray(iq)
    n = len(iq)
    rep: dict = {"n_samples": int(n), "sample_rate": sample_rate}

    if n == 0:
        return {"ok": False, "problems": ["empty IQ"], **rep}

    if np.iscomplexobj(iq):
        finite = np.isfinite(iq.real) & np.isfinite(iq.imag)
    else:
        finite = np.isfinite(iq)
    nbad = int((~finite).sum())
    if nbad:
        problems.append(f"{nbad} non-finite samples")

    i = iq.real.astype(np.float64)
    q = iq.imag.astype(np.float64)
    fs = _FULL_SCALE.get(sample_format, max(1.0, float(np.max(np.abs(iq)) or 1.0)))
    peak = float(np.max(np.abs(np.concatenate([i, q]))))
    rep["peak"] = peak
    rep["full_scale"] = fs
    if peak > fs * 1.0001:
        problems.append(f"peak {peak:.1f} exceeds {sample_format} full scale {fs:.0f}")
    at_fs = float(np.mean((np.abs(i) >= fs) | (np.abs(q) >= fs)))
    rep["clipped_fraction"] = at_fs
    if at_fs > 0.01:
        problems.append(f"{at_fs*100:.2f}% of samples at full scale")

    i_rms = float(np.sqrt(np.mean(i ** 2))) or 1e-9
    q_rms = float(np.sqrt(np.mean(q ** 2))) or 1e-9
    rep["dc_i"] = float(np.mean(i))
    rep["dc_q"] = float(np.mean(q))
    if abs(rep["dc_i"]) > 0.05 * i_rms or abs(rep["dc_q"]) > 0.05 * q_rms:
        problems.append("DC offset exceeds 5% of RMS")
    rep["iq_power_ratio_db"] = float(20.0 * np.log10(i_rms / q_rms))
    if abs(rep["iq_power_ratio_db"]) > 1.0:
        problems.append(f"I/Q power imbalance {rep['iq_power_ratio_db']:.2f} dB")
    rep["iq_correlation"] = float(np.mean(i * q) / (i_rms * q_rms))
    if abs(rep["iq_correlation"]) > 0.2:
        problems.append(f"I/Q correlation {rep['iq_correlation']:.2f} (quadrature error)")

    if np.iscomplexobj(iq):
        rep["occupied_bw_hz"] = _occupied_bandwidth(iq.astype(np.complex64), sample_rate)
        if rep["occupied_bw_hz"] > sample_rate:
            problems.append("occupied bandwidth exceeds sample rate (aliasing)")

    if expected_duration_s is not None:
        want = int(round(expected_duration_s * sample_rate))
        rep["expected_samples"] = want
        if abs(n - want) > max(1, 0.001 * want):
            problems.append(f"sample count {n} != expected {want}")

    return {"ok": not problems, "problems": problems, **rep}


def validate_file(path, sample_format: str, sample_rate: float, *,
                  expected_duration_s: float | None = None,
                  max_samples: int | None = None) -> dict:
    itemsize = 1 if sample_format == "int8" else 2
    size = path.stat().st_size
    rep: dict = {"path": str(path), "file_bytes": size,
                 "bytes_per_complex_sample": 2 * itemsize}
    if size % (2 * itemsize) != 0:
        return {"ok": False, "problems": ["file size is not a whole number of I/Q pairs"], **rep}
    iq = inspector.read_iq(path, sample_format, max_samples=max_samples)
    dur = expected_duration_s
    if max_samples is not None:
        dur = None                    # only read a slice; duration check meaningless
    out = validate_array(iq, sample_rate, sample_format=sample_format,
                         expected_duration_s=dur)
    out.update(rep)
    out["ok"] = not out["problems"]
    return out
