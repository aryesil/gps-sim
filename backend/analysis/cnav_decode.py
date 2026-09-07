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


def demod_symbols(iq, fs, prn, *, dopp_hz=None, code_phase_chips=None):
    """Hard {0,1} convolutional symbols (50 sym/s) from a GPS L2C capture.

    Acquires with band_acquire when Doppler / code phase are not supplied,
    then prompt-correlates one CM period per symbol, removes a residual
    carrier with a degree-2 phase fit on the data-wiped samples, and hard
    -decides the real part.
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
    k = np.arange(iq.size, dtype=np.float64)
    x = iq * np.exp(-2j * np.pi * float(dopp_hz) * k / fs)

    nsym = x.size // npp
    if nsym < 4:
        return np.zeros(0, dtype=np.int8)

    # prompt replica: CM code resampled to npp samples, rotated by the
    # acquired code phase.
    t = np.arange(npp) / fs
    base_rate = _CM_CHIP_HZ * (1.0 + float(dopp_hz) / 1_227_600_000.0)
    idx = (np.floor(t * base_rate + float(code_phase_chips)).astype(np.int64)
           % _CM_LEN)
    replica = cm[idx]

    corr = np.empty(nsym, dtype=np.complex128)
    for s in range(nsym):
        seg = x[s * npp:(s + 1) * npp]
        corr[s] = np.sum(seg * replica)

    # residual carrier: degree-2 fit on the data-wiped phase (corr**2
    # cancels the +/-1 symbol), then a constant phase.
    si = np.arange(nsym, dtype=np.float64)
    ph = np.unwrap(np.angle(corr ** 2))
    deg = 2 if nsym >= 5 else 1
    coef = np.polyfit(si, ph, deg) / 2.0
    corr = corr * np.exp(-1j * np.polyval(coef, si))
    corr = corr * np.exp(-1j * 0.5 * np.angle(np.sum(corr ** 2)))

    return (np.real(corr) < 0).astype(np.int8)
