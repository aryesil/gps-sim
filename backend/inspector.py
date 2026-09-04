from __future__ import annotations

import numpy as np

from backend import config

_G2_TAPS = {
    1: (2, 6), 2: (3, 7), 3: (4, 8), 4: (5, 9), 5: (1, 9), 6: (2, 10),
    7: (1, 8), 8: (2, 9), 9: (3, 10), 10: (2, 3), 11: (3, 4), 12: (5, 6),
    13: (6, 7), 14: (7, 8), 15: (8, 9), 16: (9, 10), 17: (1, 4), 18: (2, 5),
    19: (3, 6), 20: (4, 7), 21: (5, 8), 22: (6, 9), 23: (1, 3), 24: (4, 6),
    25: (5, 7), 26: (6, 8), 27: (7, 9), 28: (8, 10), 29: (1, 6), 30: (2, 7),
    31: (3, 8), 32: (4, 9),
}


def ca_code(prn: int) -> np.ndarray:
    g1 = [1] * 10
    g2 = [1] * 10
    t1, t2 = _G2_TAPS[prn]
    out = np.empty(1023, dtype=np.int8)
    for i in range(1023):
        out[i] = 1 - 2 * (g1[9] ^ (g2[t1 - 1] ^ g2[t2 - 1]))
        fb1 = g1[2] ^ g1[9]
        fb2 = g2[1] ^ g2[2] ^ g2[5] ^ g2[7] ^ g2[8] ^ g2[9]
        g1 = [fb1] + g1[:9]
        g2 = [fb2] + g2[:9]
    return out


def read_iq(path, sample_format: str, max_samples: int | None = None) -> np.ndarray:
    dtype = np.int8 if sample_format == "int8" else np.int16
    count = -1 if max_samples is None else 2 * max_samples
    raw = np.fromfile(path, dtype=dtype, count=count).astype(np.float32)
    raw = raw[: len(raw) - (len(raw) % 2)]
    return (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)


def spectrum(iq: np.ndarray, sample_rate: float, nfft: int = 4096):
    seg = iq[:nfft] if len(iq) >= nfft else np.pad(iq, (0, nfft - len(iq)))
    X = np.fft.fftshift(np.fft.fft(seg * np.hanning(len(seg))))
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / sample_rate))
    power_db = 20 * np.log10(np.abs(X) + 1e-9)
    return freqs, power_db


def acquire(iq, sample_rate, prn, doppler_range=(-6000, 6000),
            doppler_step=250.0, coherent_ms=1, noncoherent=10) -> dict:
    ncoh = int(sample_rate * 1e-3 * coherent_ms)
    need = ncoh * noncoherent
    seg = iq[:need]
    if len(seg) < need:
        noncoherent = max(1, len(seg) // ncoh)
        seg = seg[: ncoh * noncoherent]
    tt = np.arange(ncoh) / sample_rate
    idx = np.floor(tt * config.CA_CHIP_HZ).astype(int) % 1023
    local = ca_code(prn).astype(np.float64)[idx]
    LOC = np.conj(np.fft.fft(local))
    dopps = np.arange(doppler_range[0], doppler_range[1] + 1, doppler_step)
    best = (-1.0, 0.0, 0, 0)
    acc_noise = []
    dopp_peaks = []
    for di, fd in enumerate(dopps):
        acc = np.zeros(ncoh)
        for k in range(noncoherent):
            blk = seg[k * ncoh:(k + 1) * ncoh]
            blk = blk * np.exp(-1j * 2 * np.pi * fd * np.arange(k * ncoh, (k + 1) * ncoh) / sample_rate)
            acc += np.abs(np.fft.ifft(np.fft.fft(blk) * LOC)) ** 2
        acc_noise.append(acc.mean())
        pk = acc.max()
        dopp_peaks.append(np.sqrt(pk))
        if pk > best[0]:
            best = (pk, fd, int(acc.argmax()), di)
    peak, fd_hat, si, dopp_idx = best
    # 3-point parabolic interpolation of the Doppler estimate (skip at grid edges).
    if 0 < dopp_idx < len(dopp_peaks) - 1:
        p0, p1, p2 = dopp_peaks[dopp_idx - 1], dopp_peaks[dopp_idx], dopp_peaks[dopp_idx + 1]
        den = p0 - 2 * p1 + p2
        delta = 0.5 * (p0 - p2) / den if abs(den) > 1e-12 else 0.0
        fd_hat = float(doppler_range[0] + (dopp_idx + delta) * doppler_step)
    floor = float(np.mean(acc_noise))
    chip = (si * config.CA_CHIP_HZ / sample_rate) % 1023
    return {
        "prn": prn, "doppler_hz": float(fd_hat),
        "code_phase_chips": float(chip),
        "metric_db": float(10 * np.log10(peak / floor)),
    }


def correlation_curve(iq, sample_rate, prn, fd_hz, coherent_ms=1, noncoherent=10):
    """Full non-coherently-summed correlation magnitude vs code phase, at a
    fixed (already-estimated) Doppler -- acquire() only keeps the scalar peak
    of this curve; this recomputes it for the frontend's correlation-curve
    plot (one FFT-correlate pass, same cost as one Doppler bin of acquire()).
    """
    ncoh = int(sample_rate * 1e-3 * coherent_ms)
    need = ncoh * noncoherent
    seg = iq[:need]
    if len(seg) < need:
        noncoherent = max(1, len(seg) // ncoh)
        seg = seg[: ncoh * noncoherent]
    tt = np.arange(ncoh) / sample_rate
    idx = np.floor(tt * config.CA_CHIP_HZ).astype(int) % 1023
    local = ca_code(prn).astype(np.float64)[idx]
    LOC = np.conj(np.fft.fft(local))
    acc = np.zeros(ncoh)
    for k in range(noncoherent):
        blk = seg[k * ncoh:(k + 1) * ncoh]
        blk = blk * np.exp(-1j * 2 * np.pi * fd_hz * np.arange(k * ncoh, (k + 1) * ncoh) / sample_rate)
        acc += np.abs(np.fft.ifft(np.fft.fft(blk) * LOC)) ** 2
    chips = (np.arange(ncoh) * config.CA_CHIP_HZ / sample_rate) % 1023
    return chips, np.sqrt(acc)


def compare(iq, sample_rate, geometry_entries: list[dict]) -> list[dict]:
    rows = []
    for ent in geometry_entries:
        res = acquire(iq, sample_rate, ent["prn"])
        exp_c = ent["code_phase_chips"] % 1023
        err_c = ((res["code_phase_chips"] - exp_c + 511.5) % 1023) - 511.5
        rows.append({
            "prn": ent["prn"],
            "expected_code_phase_chips": exp_c,
            "measured_code_phase_chips": res["code_phase_chips"],
            "code_phase_err_chips": err_c,
            "expected_doppler_hz": ent["carrier_doppler_hz"],
            "measured_doppler_hz": res["doppler_hz"],
            "doppler_err_hz": res["doppler_hz"] - ent["carrier_doppler_hz"],
            "metric_db": res["metric_db"],
        })
    return rows
