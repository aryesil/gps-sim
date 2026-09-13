"""Clean-channel F/NAV decoder -- the inverse of
backend.analysis.fnav_encode.

Page framing (12-symbol uncoded preamble + 8x61-deinterleaved rate-1/2
K=7 Viterbi-free decode from state 0), CRC-24Q check, word type 1-4 field
extraction via the shared ``_fnav_layout`` table, and Keplerian-ephemeris
reconstruction from a CRC-valid word 1+2+3 (+4) set.

``demod_symbols_e5a`` pulls the 50 sym/s F/NAV symbol stream out of a
Galileo E5a IQ capture: E5a-I's 10230-chip/10.23 Mcps primary with the
CS20 secondary mirrors GPS L5's I5/NH10 structure exactly, so this reuses
``cnav_decode._demod`` verbatim (E5a-specific code/secondary/carrier
centre only).
"""
from __future__ import annotations

import math

import numpy as np

from backend.analysis import _fnav_layout as L
from backend.analysis._crc import crc24q

_PREAMBLE = [1, 0, 1, 1, 0, 1, 1, 1, 0, 0, 0, 0]
_G1 = 0o171
_G2 = 0o133
_PI = math.pi


def _parity(x: int) -> int:
    return bin(x).count("1") & 1


def deinterleave_8x61(sym) -> list[int]:
    a = np.asarray(list(sym)).reshape(61, 8)
    return a.T.reshape(L.CODED_SYMS).tolist()


def viterbi_free_decode(coded) -> list[int]:
    """Exact inverse of fnav_encode.conv_encode on a clean channel (from
    reg 0)."""
    reg = 0
    out: list[int] = []
    coded = list(int(s) & 1 for s in coded)
    for i in range(0, len(coded) - 1, 2):
        c1, c2 = coded[i], coded[i + 1]
        for cand in (0, 1):
            r = ((reg << 1) | cand) & 0x7F
            if _parity(r & _G1) == c1 and (_parity(r & _G2) ^ 1) == c2:
                out.append(cand)
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


def _decode_word(wtype: int, message: list[int]) -> dict:
    out: dict = {}
    off = 0
    for name, kind, scale, n in L.LAYOUT[wtype]:
        if name != "_reserved":
            out[name] = _field(message, off, kind, scale, n)
        off += n
    return out


def decode_messages(sym01) -> list[dict]:
    """Scan a demodulated {0,1} symbol stream for F/NAV pages: since the
    12-symbol preamble is uncoded (not FEC-protected), pages are found by
    a literal pattern match rather than the CNAV-style continuous re-sync
    on a decoded bit stream. Both overall polarities are tried (the
    physical layer's carrier-phase ambiguity)."""
    sym01 = list(int(x) & 1 for x in sym01)
    n = len(sym01)
    for flip in (0, 1):
        stream = [x ^ flip for x in sym01]
        msgs: list[dict] = []
        i = 0
        while i + L.PAGE_SYMS <= n:
            if stream[i:i + L.PREAMBLE_SYMS] == _PREAMBLE:
                coded = deinterleave_8x61(stream[i + L.PREAMBLE_SYMS:i + L.PAGE_SYMS])
                pre_fec = viterbi_free_decode(coded)
                message = pre_fec[:L.MSG_BITS]
                crc = pre_fec[L.MSG_BITS:L.MSG_BITS + L.CRC_BITS]
                if crc24q(message) == crc:
                    wtype = _u(message, 0, 6)
                    msgs.append({
                        "type": wtype,
                        "prn": _u(message, 6, 6) if wtype == 1 else None,
                        "crc_ok": True,
                        "fields": (_decode_word(wtype, message)
                                   if wtype in L.LAYOUT else {}),
                    })
                    i += L.PAGE_SYMS
                    continue
            i += 1
        if msgs:
            return msgs
    return []


def reconstruct_ephemeris(msgs: list[dict]) -> dict:
    by_type: dict[int, dict] = {}
    for m in msgs:
        if m.get("crc_ok") and m["type"] in (1, 2, 3, 4):
            by_type[m["type"]] = m["fields"]
    if not ({1, 2, 3} <= set(by_type)):
        raise ValueError("need CRC-valid F/NAV word types 1, 2 and 3")
    f1, f2, f3 = by_type[1], by_type[2], by_type[3]
    f4 = by_type.get(4, {})
    rec = {"system": "E"}
    rec["sqrtA"] = f2["sqrtA"]
    rec["e"] = f2["e"]
    rec["m0"] = f2["m0"] * _PI
    rec["omega_dot"] = f2["omega_dot"] * _PI
    rec["omega0"] = f2["omega0"] * _PI
    rec["idot"] = f2["idot"] * _PI
    rec["i0"] = f3["i0"] * _PI
    rec["omega"] = f3["omega"] * _PI
    rec["delta_n"] = f3["delta_n"] * _PI
    for k in ("cuc", "cus", "crc", "crs"):
        rec[k] = f3[k]
    rec["toe"] = f3["toe"]
    rec["cic"] = f4.get("cic", 0.0)
    rec["cis"] = f4.get("cis", 0.0)
    rec["toc"] = f1["toc"]
    rec["af0"] = f1["af0"]
    rec["af1"] = f1["af1"]
    rec["af2"] = f1["af2"]
    rec["tgd"] = f1["tgd"]
    rec["gal_week"] = int(f1.get("week", 0))
    return rec


# --- IQ -> convolutional symbols ---------------------------------------------

_E5A_LEN = 10230
_E5A_CHIP_HZ = 10.23e6
_E5A_CTR_HZ = 1_176_450_000.0
# Galileo E5a-I CS20 secondary (Galileo OS SIS ICD / GNSS-SDR
# GALILEO_E5A_I_SECONDARY_CODE), {+1,-1}, same for every PRN.
_CS20 = np.array([1 if b == "0" else -1 for b in "10000100001011101001"],
                 dtype=np.float64)


def demod_symbols_e5a(iq, fs, prn, *, dopp_hz=None, code_phase_chips=None):
    """Hard {0,1} F/NAV symbols (50 sym/s) from a Galileo E5a capture: the
    E5a-I primary at 10.23 Mcps with the CS20 secondary wiped off. Reuses
    ``cnav_decode._demod`` verbatim -- structurally identical to
    ``demod_symbols_l5`` (10230-chip primary, 20-chip 1 kHz secondary)."""
    from backend.analysis import band_acquire, cnav_decode
    from backend.synth import _lib

    iq = np.asarray(iq)
    if not np.iscomplexobj(iq):
        iq = iq.astype(np.complex64)
    elif iq.dtype == np.complex128:
        iq = iq.astype(np.complex64)
    ei, _eq = _lib.code_e5a(int(prn))
    ei = ei.astype(np.float64)
    if dopp_hz is None or code_phase_chips is None:
        a = band_acquire.acquire(iq, fs, ei, chip_hz=_E5A_CHIP_HZ,
                                 code_len=_E5A_LEN, dopp_hz=6000.0,
                                 dopp_step=100.0, nperiods=1)
        dopp_hz = a["doppler_hz"] if dopp_hz is None else dopp_hz
        code_phase_chips = (a["code_phase_chips"] if code_phase_chips is None
                            else code_phase_chips)
    return cnav_decode._demod(iq, fs, ei, chip_hz=_E5A_CHIP_HZ,
                              code_len=_E5A_LEN, carrier_ctr_hz=_E5A_CTR_HZ,
                              dopp_hz=dopp_hz, code_phase_chips=code_phase_chips,
                              sec=_CS20, sec_rate_hz=1000.0)
