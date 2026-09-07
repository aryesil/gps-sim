"""Clean-channel CNAV decoder -- the inverse of backend.analysis.cnav_encode.

Convolutional Viterbi-free decode from state 0, 0x8B framing, CRC-24Q
check, message 10 / 11 / 30 / 33 field extraction via the shared
``_cnav_layout`` table, and Keplerian-ephemeris reconstruction from a
CRC-valid 10 + 11 (+ 30) set.

``demod_symbols`` pulls the 50 sym/s convolutional symbol stream out of a
GPS L2C IQ capture: the CM code period is exactly one symbol (20 ms), so
this is a plain prompt correlation with a carrier wipe, no 20:1 bit/code
folding.
"""
from __future__ import annotations

import math

import numpy as np

from backend.analysis import _cnav_layout as L
from backend.analysis._crc import crc24q

_PREAMBLE = 0x8B
_G1 = 0o171
_G2 = 0o133
_PI = math.pi


def _parity(x: int) -> int:
    return bin(x).count("1") & 1


def viterbi_free_decode(sym) -> list[int]:
    """Exact inverse of cnav_encode._Conv on a clean channel (from reg 0)."""
    reg = 0
    out: list[int] = []
    sym = list(int(s) & 1 for s in sym)
    for i in range(0, len(sym) - 1, 2):
        s0, s1 = sym[i], sym[i + 1]
        for guess in (0, 1):
            r = ((reg << 1) | guess) & 0x7F
            if _parity(r & _G1) == s0 and (_parity(r & _G2) ^ 1) == s1:
                out.append(guess)
                reg = r
                break
        else:
            out.append(0)
            reg = (reg << 1) & 0x7F
    return out


def _u(bits, lo, n):
    v = 0
    for b in bits[lo:lo + n]:
        v = (v << 1) | (int(b) & 1)
    return v


def _field(bits, lo, kind, scale, n):
    v = _u(bits, lo, n)
    if kind == "s" and (v & (1 << (n - 1))):
        v -= 1 << n
    return v * scale


def _decode_payload(msg_type: int, payload: list[int]) -> dict:
    out: dict = {}
    off = 0
    for name, kind, scale, nbits in L.LAYOUT[msg_type]:
        out[name] = _field(payload, off, kind, scale, nbits)
        off += nbits
    return out


def decode_messages(sym01) -> list[dict]:
    sym01 = list(int(x) & 1 for x in sym01)
    for flip in (0, 1):
        stream = [x ^ flip for x in sym01]
        bits = viterbi_free_decode(stream)
        msgs: list[dict] = []
        i = 0
        while i + L.MSG_BITS <= len(bits):
            if _u(bits, i, 8) == _PREAMBLE:
                m = bits[i:i + L.MSG_BITS]
                if crc24q(m[:L.MSG_BITS - L.CRC_BITS]) == m[L.MSG_BITS - L.CRC_BITS:]:
                    mtype = _u(m, 14, 6)
                    payload = m[L.HEADER_BITS:L.MSG_BITS - L.CRC_BITS]
                    msgs.append({
                        "type": mtype,
                        "prn": _u(m, 8, 6),
                        "tow_6s": _u(m, 20, 17),
                        "alert": m[37],
                        "crc_ok": True,
                        "fields": (_decode_payload(mtype, payload)
                                   if mtype in L.LAYOUT else {}),
                    })
                    i += L.MSG_BITS
                    continue
            i += 1
        if msgs:
            return msgs
    return []


def reconstruct_ephemeris(msgs: list[dict]) -> dict:
    by_type: dict[int, dict] = {}
    for m in msgs:
        if m.get("crc_ok") and m["type"] in (10, 11, 30, 33):
            by_type[m["type"]] = m["fields"]
    if not ({10, 11} <= set(by_type)):
        raise ValueError("need CRC-valid CNAV messages 10 and 11")
    f10, f11 = by_type[10], by_type[11]
    f30 = by_type.get(30, {})
    rec = {"system": "G"}
    rec["sqrtA"] = math.sqrt(max(f10["dA"] + L.A_REF, 1.0))
    rec["e"] = f10["e"]
    rec["m0"] = f10["m0"] * _PI
    rec["delta_n"] = f10["delta_n"] * _PI
    rec["omega"] = f10["omega"] * _PI
    rec["toe"] = f11["toe"] or f10["toe"]
    rec["omega0"] = f11["omega0"] * _PI
    rec["i0"] = f11["i0"] * _PI
    rec["omega_dot"] = (f11["d_omega_dot"] + L.OMEGA_DOT_REF) * _PI
    rec["idot"] = f11["idot"] * _PI
    for k in ("cuc", "cus", "crc", "crs", "cic", "cis"):
        rec[k] = f11[k]
    rec["toc"] = f30.get("toc", rec["toe"])
    rec["af0"] = f30.get("af0", 0.0)
    rec["af1"] = f30.get("af1", 0.0)
    rec["af2"] = f30.get("af2", 0.0)
    rec["tgd"] = f30.get("tgd", 0.0)
    rec["isc_l2c"] = f30.get("isc_l2c", 0.0)
    rec["gps_week"] = int(f10.get("wn", 0))
    return rec


# --- IQ -> convolutional symbols ---------------------------------------------

_CM_LEN = 10230
_CM_CHIP_HZ = 0.5115e6
_SYM_S = _CM_LEN / _CM_CHIP_HZ          # 0.02 s -- one CNAV symbol


_SUBCHUNKS = 20            # sub-symbol partials -> +/-500 Hz residual search room


def _concentration(corr):
    """Fraction of correlator energy on the real axis after the best
    constant-phase rotation -- ~1 for a locked BPSK stream, ~0.5 for a
    spinning one. Data-blind (corr**2 cancels the +/-1 symbol)."""
    c2 = np.sum(corr ** 2)
    aligned = corr * np.exp(-1j * 0.5 * np.angle(c2))
    return float(np.sum(np.real(aligned) ** 2) / np.sum(np.abs(corr) ** 2))


def demod_symbols(iq, fs, prn, *, dopp_hz=None, code_phase_chips=None):
    """Hard {0,1} convolutional symbols (50 sym/s) from a GPS L2C capture.

    band_acquire's Doppler (100 Hz grid, 4-period non-coherent) is far too
    coarse to hold carrier phase over a 30-40 s capture -- a 100 Hz error
    spins the prompt correlator through 2 cycles per symbol and, through the
    code-rate/carrier tie, slips the code by chips. The tracker therefore:

    1. locks Doppler to < 0.1 Hz on a ~2 s prefix (a coarse concentration
       grid, then a linear data-wiped-phase fit iterated to convergence);
    2. prompt-correlates the whole capture with one continuous code + carrier
       replica at that Doppler;
    3. estimates the residual carrier phase locally, per 0.5 s block, from a
       fine concentration search on the sub-symbol partials -- drift-robust,
       needs only < 1 Hz residual at any instant -- and integrates it into a
       smooth phase(t) that is de-rotated before the hard decision.
    """
    from backend.analysis import band_acquire
    from backend.synth import _lib

    iq = np.asarray(iq, dtype=np.complex128)
    cm, _cl = _lib.code_l2c(int(prn))
    cm = cm.astype(np.float64)

    if dopp_hz is None or code_phase_chips is None:
        a = band_acquire.acquire(iq, fs, cm, chip_hz=_CM_CHIP_HZ,
                                 code_len=_CM_LEN, dopp_hz=6000.0,
                                 dopp_step=100.0)
        dopp_hz = a["doppler_hz"] if dopp_hz is None else dopp_hz
        code_phase_chips = (a["code_phase_chips"] if code_phase_chips is None
                            else code_phase_chips)

    npp = int(round(fs * _SYM_S))
    nsym = iq.size // npp
    if nsym < 4:
        return np.zeros(0, dtype=np.int8)
    m = _SUBCHUNKS
    while npp % m:
        m -= 1
    # band_acquire reports the circular correlation lag (gps-sdr-sim
    # convention); the replica's start chip is its complement.
    cp0 = (_CM_LEN - float(code_phase_chips)) % _CM_LEN
    tt = np.arange(nsym * npp, dtype=np.float64) / fs

    def _subcorr(dopp0, drift=0.0):
        """Code+carrier-wiped sub-symbol partials over the whole capture,
        shape (nsym, m). The carrier is a linear-frequency chirp
        ``dopp0 + drift*t``; the code rate is tied to it through the
        L2 carrier ratio, so both the carrier phase and the code phase are
        the exact time integrals -- code Doppler, its drift, and a
        non-integer samples-per-symbol are all tracked across the capture."""
        ramp = dopp0 * tt + 0.5 * drift * tt * tt        # integral of f(t)
        wipe = np.exp(-2j * np.pi * ramp)
        code_phase = cp0 + _CM_CHIP_HZ * tt + (
            _CM_CHIP_HZ / 1_227_600_000.0) * ramp
        idx = (np.floor(code_phase).astype(np.int64)) % _CM_LEN
        prod = (iq[:nsym * npp] * wipe * cm[idx]).reshape(nsym, m, npp // m)
        return prod.sum(axis=2)

    # --- 1. lock Doppler. band_acquire's 100 Hz grid can sit >150 Hz off,
    # which spins the correlator and, via the code-rate/carrier tie, slips
    # the code. A per-symbol concentration metric is ambiguous at multiples
    # of 25 Hz (a half-cycle-per-symbol residual just alternates the sign of
    # every symbol -- indistinguishable from data), so the coarse pull-in
    # runs on the *sub-symbol* partials: squaring cancels the +/-1 symbol
    # and leaves a tone at twice the residual carrier, unambiguous out to
    # +/- m/(2*_SYM_S) Hz. A linear data-wiped-phase fit then trims the rest.
    pn = min(nsym, 200)
    for _ in range(3):
        sq = (_subcorr(dopp_hz)[:pn].reshape(-1)) ** 2
        fr = np.fft.fftfreq(sq.size, d=_SYM_S / m)
        step = float(fr[np.argmax(np.abs(np.fft.fft(sq)))]) / 2.0
        dopp_hz += step
        if abs(step) < 1.0:
            break
    for _ in range(6):
        corr = _subcorr(dopp_hz)[:pn].sum(axis=1)
        slope = np.polyfit(np.arange(pn, dtype=np.float64),
                           np.unwrap(np.angle(corr ** 2)), 1)[0] / 2.0
        dfr = slope / (2.0 * np.pi * _SYM_S)
        dopp_hz += dfr
        if abs(dfr) < 0.02:
            break

    # --- 2. estimate the Doppler drift from the per-block residual carrier
    # frequency (FFT of the squared sub-symbol partials -> a tone at twice
    # the residual, alias-free out to +/- m/(2*_SYM_S) Hz), then rebuild the
    # replica as a linear-frequency chirp. Without this the code rate -- tied
    # to the carrier -- slips a third of a chip over 40 s at 0.9 Hz/s.
    B = 50

    def _block_freqs(p):
        nbk = max(1, p.shape[0] // B)
        out = np.zeros(nbk)
        for k in range(nbk):
            sq = (p[k * B:(k + 1) * B].reshape(-1)) ** 2
            fr = np.fft.fftfreq(sq.size, d=_SYM_S / m)
            out[k] = float(fr[np.argmax(np.abs(np.fft.fft(sq)))]) / 2.0
        return out, nbk

    p_full = _subcorr(dopp_hz)
    fblk, nb = _block_freqs(p_full)
    if nb > 3:
        bt_s = (np.arange(nb) + 0.5) * B * _SYM_S
        c1, c0 = np.polyfit(bt_s, fblk, 1)
        dopp_hz += float(c0)
        p_full = _subcorr(dopp_hz, float(c1))

    # --- 3. residual carrier phase. After the chirp rebuild the residual is
    # small and smooth, so a plain unwrap of the data-wiped phase (corr**2
    # cancels the +/-1 symbol) fitted to a low-order polynomial is safe.
    si = np.arange(nsym, dtype=np.float64)
    corr = p_full.sum(axis=1)
    deg = min(4, nsym - 1) if nsym >= 5 else 1
    coef = np.polyfit(si, np.unwrap(np.angle(corr ** 2)), deg) / 2.0
    corr = corr * np.exp(-1j * np.polyval(coef, si))
    corr = corr * np.exp(-1j * 0.5 * np.angle(np.sum(corr ** 2)))

    return (np.real(corr) < 0).astype(np.int8)
