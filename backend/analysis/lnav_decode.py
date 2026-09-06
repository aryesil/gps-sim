"""Inverse of :mod:`backend.analysis.lnav_encode`.

Demodulate the 50 bps LNAV symbol stream out of native-engine IQ, find
subframe boundaries with a parity check, and reconstruct an ephemeris
dict shaped like :func:`backend.ephem.ephemeris.parse_rinex` output.
"""
from __future__ import annotations

import math

import numpy as np

from backend.analysis import lnav_encode as le


def check_parity(word30, D29_prev: int, D30_prev: int) -> bool:
    w = list(word30)
    d_src = [b ^ D30_prev for b in w[:24]]
    return list(w[24:]) == le.parity_bits(d_src, D29_prev, D30_prev)


def _u(bits) -> int:
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def _s(bits) -> int:
    v = _u(bits)
    n = len(bits)
    return v - (1 << n) if v >= 1 << (n - 1) else v


def _src_words(words) -> list[list[int]]:
    """10 lists of 24 source data bits, undoing the D30* data inversion."""
    d29 = d30 = 0
    out = []
    for w in words:
        out.append([b ^ d30 for b in w[:24]])
        d29, d30 = w[28], w[29]
    return out


def find_frame(bits) -> list[dict]:
    """Scan a symbol/bit sequence for parity-valid 300-bit subframes.

    Accepts {0,1} bits or {+1,-1} symbols (with +1 -> bit 0). Handles a
    globally inverted stream. Returns one dict per hit with keys
    ``offset``, ``inverted``, ``subframe_id``, ``tow_count``, ``words``
    (10 raw 30-bit words).
    """
    seq = np.asarray(list(bits))
    if seq.size and set(np.unique(seq)).issubset({-1, 1}):
        b = [(0 if x > 0 else 1) for x in seq]
    else:
        b = [int(x) & 1 for x in seq]
    pre = le.PREAMBLE
    inv = [1 - x for x in pre]
    hits: list[dict] = []
    i = 0
    n = len(b)
    while i + 300 <= n:
        seg = b[i:i + 8]
        inverted = seg == inv
        if seg != pre and not inverted:
            i += 1
            continue
        raw = [1 - x for x in b[i:i + 300]] if inverted else b[i:i + 300]
        words = [raw[k * 30:(k + 1) * 30] for k in range(10)]
        d29 = d30 = 0
        ok = True
        for w in words:
            if not check_parity(w, d29, d30):
                ok = False
                break
            d29, d30 = w[28], w[29]
        if not ok:
            i += 1
            continue
        how = _src_words(words)[1]
        hits.append({
            "offset": i, "inverted": inverted,
            "subframe_id": _u(how[19:22]), "tow_count": _u(how[0:17]),
            "words": words,
        })
        i += 300
    return hits


_SC = math.pi  # semicircle -> radian


def decode_ephemeris(subframes: dict) -> dict:
    """Reconstruct clock + Keplerian ephemeris from subframes 1, 2, 3.

    ``subframes`` maps subframe id -> list of 10 raw 30-bit words.
    """
    if not {1, 2, 3} <= set(subframes):
        raise ValueError("need subframes 1, 2 and 3")
    d1 = _src_words(subframes[1])
    d2 = _src_words(subframes[2])
    d3 = _src_words(subframes[3])
    eph: dict = {}

    # subframe 1 -- clock
    eph["gps_week"] = _u(d1[2][0:10])
    eph["health"] = _u(d1[2][12:18])
    eph["iodc"] = (_u(d1[2][22:24]) << 8) | _u(d1[7][0:8])
    eph["tgd"] = _s(d1[6][16:24]) * 2 ** -31
    eph["toc"] = _u(d1[7][8:24]) * 16.0
    eph["af2"] = _s(d1[8][0:8]) * 2 ** -55
    eph["af1"] = _s(d1[8][8:24]) * 2 ** -43
    eph["af0"] = _s(d1[9][0:22]) * 2 ** -31

    # subframe 2 -- ephemeris part 1
    eph["iode"] = _u(d2[2][0:8])
    eph["crs"] = _s(d2[2][8:24]) * 2 ** -5
    eph["delta_n"] = _s(d2[3][0:16]) * 2 ** -43 * _SC
    eph["m0"] = _s(d2[3][16:24] + d2[4][0:24]) * 2 ** -31 * _SC
    eph["cuc"] = _s(d2[5][0:16]) * 2 ** -29
    eph["e"] = _u(d2[5][16:24] + d2[6][0:24]) * 2 ** -33
    eph["cus"] = _s(d2[7][0:16]) * 2 ** -29
    eph["sqrtA"] = _u(d2[7][16:24] + d2[8][0:24]) * 2 ** -19
    eph["toe"] = _u(d2[9][0:16]) * 16.0

    # subframe 3 -- ephemeris part 2
    eph["cic"] = _s(d3[2][0:16]) * 2 ** -29
    eph["omega0"] = _s(d3[2][16:24] + d3[3][0:24]) * 2 ** -31 * _SC
    eph["cis"] = _s(d3[4][0:16]) * 2 ** -29
    eph["i0"] = _s(d3[4][16:24] + d3[5][0:24]) * 2 ** -31 * _SC
    eph["crc"] = _s(d3[6][0:16]) * 2 ** -5
    eph["omega"] = _s(d3[6][16:24] + d3[7][0:24]) * 2 ** -31 * _SC
    eph["omega_dot"] = _s(d3[8][0:24]) * 2 ** -43 * _SC
    eph["idot"] = _s(d3[9][8:22]) * 2 ** -43 * _SC
    return eph


_L1_HZ = 1575.42e6


def _prompt_per_ms(x_wiped, fs, ca, code_phase0_chips, code_rate):
    """Complex prompt correlator output, one sample per 1 ms C/A period,
    for a carrier-wiped signal and a given code chipping rate."""
    k = np.arange(x_wiped.size, dtype=np.float64)
    # acquire() reports the peak as a right shift of the local replica, so
    # the aligned replica is the nominal code advanced by -code_phase0.
    idx = np.floor(k * code_rate / fs - code_phase0_chips).astype(np.int64) % 1023
    corr = x_wiped * ca[idx]
    spms = fs / 1000.0
    m = int(x_wiped.size // spms)
    edges = np.floor(np.arange(m + 1) * spms).astype(np.int64)
    csum = np.concatenate(([0.0 + 0j], np.cumsum(corr)))
    return csum[edges[1:]] - csum[edges[:-1]]


def demod_nav_bits(iq, fs, prn, code_phase0_chips, doppler_hz, *,
                   n_bits: int = 1500, align_ms: int = 0):
    """Hard nav bits (``int8`` {0,1}) from IQ.

    Steps: prompt correlation to 1 ms samples, residual-frequency search,
    BPSK phase de-rotation, then 20 ms integration. ``align_ms`` skips a
    leading partial bit so the 20 ms blocks land on transmitted bit
    boundaries. Global sign ambiguity is left for :func:`find_frame`.
    """
    from backend import inspector

    chip_hz = float(inspector.config.CA_CHIP_HZ)
    ca = inspector.ca_code(prn).astype(np.float64)
    x = np.asarray(iq).astype(np.complex128)
    k = np.arange(x.size, dtype=np.float64)
    x = x * np.exp(-2j * np.pi * doppler_hz * k / fs)

    # The engine holds Doppler constant, but the acquired carrier estimate
    # carries a small error; the matching code-rate error walks the replica
    # off over a 20 s capture. Grid-search the residual chipping rate for
    # peak prompt energy.
    base_rate = chip_hz * (1.0 + doppler_hz / _L1_HZ)
    best_p, best_e = None, -1.0
    for drate in np.linspace(-3.0, 3.0, 31):
        cand = _prompt_per_ms(x, fs, ca, code_phase0_chips, base_rate + drate)
        e = float(np.sum(np.abs(cand)))
        if e > best_e:
            best_e, best_p = e, cand
    p = best_p
    if align_ms:
        p = p[align_ms:]
    nb = min(n_bits, p.size // 20)
    p = p[:nb * 20]
    if nb == 0:
        return np.zeros(0, dtype=np.int8)

    # residual carrier frequency: coarse 1 ms-rate search, then a fine
    # parabolic refine (the phase must stay coherent across the whole
    # capture, so sub-0.05 Hz accuracy matters over 20 s).
    m = np.arange(p.size, dtype=np.float64)

    def _mag(f):
        return np.abs(np.sum(p * np.exp(-2j * np.pi * f * m / 1000.0)))

    grid = np.arange(-45.0, 45.01, 0.25)
    mags = np.array([_mag(f) for f in grid])
    j = int(np.argmax(mags))
    best_f = grid[j]
    if 0 < j < len(grid) - 1:
        y0, y1, y2 = mags[j - 1], mags[j], mags[j + 1]
        den = y0 - 2 * y1 + y2
        if abs(den) > 1e-12:
            best_f += 0.25 * 0.5 * (y0 - y2) / den
    p = p * np.exp(-2j * np.pi * best_f * m / 1000.0)

    bits = p.reshape(nb, 20).sum(axis=1)

    # second-stage residual from the phase ramp of the data-wiped bit
    # samples (bits**2 removes the +/-1 modulation), then a constant phase.
    bi = np.arange(nb, dtype=np.float64)
    ph = np.unwrap(np.angle(bits ** 2))
    slope = np.polyfit(bi, ph, 1)[0] / 2.0        # rad per bit
    bits = bits * np.exp(-1j * slope * bi)
    phi = 0.5 * np.angle(np.sum(bits ** 2))
    bits = bits * np.exp(-1j * phi)
    return (np.real(bits) < 0).astype(np.int8)
