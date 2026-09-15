"""Clean-channel B-CNAV2 decoder -- the inverse of
backend.analysis.bcnav2_encode.

Frame framing (24-symbol uncoded preamble literal match, both overall
polarities tried -- the physical layer's carrier-phase ambiguity),
LDPC(576,288) decode via ``backend.analysis.ldpc``, CRC-24Q check,
message type 10/11/30 field extraction via ``_bcnav2_layout``, and a
Keplerian-ephemeris reconstruction from a CRC-valid MT10+MT11 (+MT30) set.

``demod_symbols_b2a`` pulls the 200 sym/s B-CNAV2 symbol stream out of a
BeiDou B2a-data IQ capture: same 10230-chip/10.23 Mcps primary-code family
as GPS L5 / Galileo E5a, but a 5-chip secondary at a 200 Hz (not 50 Hz)
symbol rate, so it calls ``cnav_decode._demod`` with an explicit
``sym_s=0.005`` rather than reusing its 50 sym/s default.
"""
from __future__ import annotations

import math

import numpy as np

from backend.analysis import _bcnav2_layout as L
from backend.analysis import ldpc
from backend.analysis._crc import crc24q

_PREAMBLE = [1, 1, 1, 0, 0, 0, 1, 0, 0, 1, 0, 0,
             1, 1, 0, 1, 1, 1, 1, 0, 1, 0, 0, 0]
_FRAME_SYMS = 24 + 576
_DATA_BITS = 264
_PI = math.pi


def _u(bits, lo, n):
    v = 0
    for b in bits[lo:lo + n]:
        v = (v << 1) | (int(b) & 1)
    return v


def _field(bits, off, n, kind, scale):
    v = _u(bits, off, n)
    if kind == "s" and (v & (1 << (n - 1))):
        v -= 1 << n
    return v if scale is None else v * scale


def _decode_fields(mtype: int, info: list[int]) -> dict:
    out: dict = {}
    for name, off, n, kind, scale in L.FIELDS_BY_TYPE.get(mtype, []):
        out[name] = _field(info, off, n, kind, scale)
    return out


def decode_messages(sym01) -> list[dict]:
    """Scan a demodulated {0,1} symbol stream for B-CNAV2 frames."""
    sym01 = list(int(x) & 1 for x in sym01)
    n = len(sym01)
    for flip in (0, 1):
        stream = [x ^ flip for x in sym01]
        msgs: list[dict] = []
        i = 0
        while i + _FRAME_SYMS <= n:
            if stream[i:i + 24] == _PREAMBLE:
                coded = stream[i + 24:i + _FRAME_SYMS]
                info, ldpc_ok = ldpc.decode(coded)
                crc_ok = crc24q(info[:_DATA_BITS]) == info[_DATA_BITS:288]
                if crc_ok:
                    mtype = _u(info, 6, 6)
                    msgs.append({
                        "type": mtype,
                        "prn": _u(info, 0, 6),
                        "sow": _u(info, 12, 18) * 3,
                        "ldpc_ok": ldpc_ok,
                        "crc_ok": True,
                        "fields": _decode_fields(mtype, info),
                    })
                    i += _FRAME_SYMS
                    continue
            i += 1
        if msgs:
            return msgs
    return []


def reconstruct_ephemeris(msgs: list[dict]) -> dict:
    by_type: dict[int, dict] = {}
    for m in msgs:
        if m.get("crc_ok") and m["type"] in (L.MSG_EPH1, L.MSG_EPH2, L.MSG_CLK_IONO):
            by_type[m["type"]] = m["fields"]
    if not ({L.MSG_EPH1, L.MSG_EPH2} <= set(by_type)):
        raise ValueError("need CRC-valid B-CNAV2 messages 10 and 11")
    f10, f11 = by_type[L.MSG_EPH1], by_type[L.MSG_EPH2]
    f30 = by_type.get(L.MSG_CLK_IONO, {})
    sat_type = int(f10.get("sat_type", 3))
    a0 = L.a_ref_for(sat_type) + f10["delta_a"]
    rec = {"system": "C"}
    rec["sqrtA"] = math.sqrt(max(a0, 1.0))
    rec["e"] = f10["e"]
    rec["m0"] = f10["m0"]
    rec["delta_n"] = f10["delta_n0"]
    rec["omega"] = f10["omega"]
    rec["toe"] = f10["toe"]
    rec["omega0"] = f11["omega0"]
    rec["i0"] = f11["i0"]
    rec["omega_dot"] = f11["omega_dot"]
    rec["idot"] = f11["idot"]
    for k in ("cuc", "cus", "crc", "crs", "cic", "cis"):
        rec[k] = f11[k]
    rec["toc"] = f30.get("toc", rec["toe"])
    rec["af0"] = f30.get("af0", 0.0)
    rec["af1"] = f30.get("af1", 0.0)
    rec["af2"] = f30.get("af2", 0.0)
    rec["tgd_b2ap"] = f30.get("tgd_b2ap", 0.0)
    rec["isc_b2ad"] = f30.get("isc_b2ad", 0.0)
    rec["iode"] = int(f10.get("iode", 0))
    rec["bds_week"] = int(f10.get("wn", 0))
    return rec


# --- IQ -> B-CNAV2 symbols ---------------------------------------------

_B2A_LEN = 10230
_B2A_CHIP_HZ = 10.23e6
_B2A_CTR_HZ = 1_176_450_000.0
_SYM_S = 0.005                                   # 200 sym/s
# BDS-SIS-ICD-B2a-1.0 data-component 5-chip secondary "00010" (MSB first),
# bit 0 -> chip +1, bit 1 -> chip -1 (same convention as L5's NH10 / E5a's
# CS20).
_SEC5 = np.array([1 if b == "0" else -1 for b in "00010"], dtype=np.float64)


def demod_symbols_b2a(iq, fs, prn, *, dopp_hz=None, code_phase_chips=None):
    """Hard {0,1} B-CNAV2 symbols (200 sym/s) from a BeiDou B2a-data
    capture: the B2a-data primary at 10.23 Mcps with the 5-chip secondary
    wiped off."""
    from backend.analysis import band_acquire, cnav_decode
    from backend.synth import _lib

    iq = np.asarray(iq)
    if not np.iscomplexobj(iq):
        iq = iq.astype(np.complex64)
    elif iq.dtype == np.complex128:
        iq = iq.astype(np.complex64)
    bd, _bp = _lib.code_b2a(int(prn))
    bd = bd.astype(np.float64)
    if dopp_hz is None or code_phase_chips is None:
        a = band_acquire.acquire(iq, fs, bd, chip_hz=_B2A_CHIP_HZ,
                                 code_len=_B2A_LEN, dopp_hz=6000.0,
                                 dopp_step=100.0, nperiods=1)
        dopp_hz = a["doppler_hz"] if dopp_hz is None else dopp_hz
        code_phase_chips = (a["code_phase_chips"] if code_phase_chips is None
                            else code_phase_chips)
    return cnav_decode._demod(iq, fs, bd, chip_hz=_B2A_CHIP_HZ,
                              code_len=_B2A_LEN, carrier_ctr_hz=_B2A_CTR_HZ,
                              dopp_hz=dopp_hz, code_phase_chips=code_phase_chips,
                              sec=_SEC5, sec_rate_hz=1000.0, sym_s=_SYM_S)
