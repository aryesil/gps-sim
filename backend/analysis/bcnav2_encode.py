"""BeiDou B-CNAV2 navigation-message encoder (BDS-SIS-ICD-B2a-1.0 Section
6), carried on the B2a-data component.

Frame structure (real, from GNSS-SDR's merged B-CNAV2 support -- see
``_bcnav2_layout.py``'s module docstring): 600 symbols every 3 s at
200 sym/s = a 24-symbol uncoded preamble (``111000100100110111101000``,
hex 0xE24DE8) followed by 576 symbols carrying 288 LDPC(576,288)-encoded
info bits (264 data bits + a trailing CRC-24Q). The LDPC layer is
``backend.analysis.ldpc`` -- see that module's docstring for why it is a
documented self-consistent compromise rather than the ICD's real GF(64)
non-binary code.

Message types 10 (Ephemeris I), 11 (Ephemeris II) and 30 (clock + B2a
group-delay/inter-signal-correction) are cycled every 9 s (three 3 s
frames), matching the real receiver's cross-frame linkage requirement
(MT11 must immediately follow MT10, same IODE).

Bit -> chip convention matches the rest of the CNAV family in this repo
(``cnav_encode.py``, reused verbatim by ``cnav_decode._demod``'s hard
decision rule): bit 0 -> chip +1, bit 1 -> chip -1.
"""
from __future__ import annotations

import math

import numpy as np

from backend.analysis import _bcnav2_layout as L
from backend.analysis import ldpc
from backend.analysis._crc import crc24q
from backend.geometry import _BDS_GEO_PRNS

_PREAMBLE = [1, 1, 1, 0, 0, 0, 1, 0, 0, 1, 0, 0,
             1, 1, 0, 1, 1, 1, 1, 0, 1, 0, 0, 0]   # 24 uncoded symbols
_DATA_BITS = 264                                     # info bits before CRC (288 - 24 CRC)


def _f(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(v) or math.isinf(v) else v


def _sat_type_for_prn(prn: int) -> int:
    """1 (GEO) / 3 (MEO) per GNSS-SDR's sat_type encoding. This repo has no
    separate IGSO PRN range (see geometry.py's _BDS_GEO_PRNS), so every
    non-GEO PRN encodes as MEO (sat_type=3); geometry.py's own GEO/MEO
    propagation split is driven by PRN + inclination, not by this field --
    sat_type here only feeds A0 = a_ref(sat_type) + delta_a below."""
    return 1 if int(prn) in _BDS_GEO_PRNS else 3


def _write_field(bits: list[int], off: int, n: int, kind: str, scale, value) -> None:
    if scale is None:
        q = int(value)
    else:
        q = int(round(_f(value) / scale))
    if kind == "s":
        lo, hi = -(1 << (n - 1)), (1 << (n - 1)) - 1
        q = max(lo, min(hi, q))
        q &= (1 << n) - 1
    else:
        q = max(0, min((1 << n) - 1, int(q)))
    for i in range(n):
        bits[off + i] = (q >> (n - 1 - i)) & 1


def _values_mt10(eph: dict, prn: int, week: int) -> dict:
    sat_type = _sat_type_for_prn(prn)
    a0 = eph.get("sqrtA", 0.0)
    a0 = _f(a0) ** 2 if a0 else 0.0
    delta_a = a0 - L.a_ref_for(sat_type) if a0 else 0.0
    return {
        "wn": int(week) & 0x1FFF,
        "iode": int(_f(eph.get("iode", 0))) & 0xFF,
        "toe": eph.get("toe", 0.0),
        "sat_type": sat_type,
        "delta_a": delta_a,
        "a_dot": 0.0,       # semi-major-axis rate -- negligible over a fix, dropped
        "delta_n0": eph.get("delta_n", 0.0),
        "delta_n0_dot": 0.0,  # second derivative -- negligible, dropped
        "m0": eph.get("m0", 0.0),
        "e": eph.get("e", 0.0),
        "omega": eph.get("omega", 0.0),
    }


def _values_mt11(eph: dict) -> dict:
    return {
        "hs": 0,
        "omega0": eph.get("omega0", 0.0),
        "i0": eph.get("i0", 0.0),
        "omega_dot": eph.get("omega_dot", 0.0),
        "idot": eph.get("idot", 0.0),
        "cis": eph.get("cis", 0.0),
        "cic": eph.get("cic", 0.0),
        "crs": eph.get("crs", 0.0),
        "crc": eph.get("crc", 0.0),
        "cus": eph.get("cus", 0.0),
        "cuc": eph.get("cuc", 0.0),
    }


def _values_mt30(eph: dict) -> dict:
    return {
        "hs": 0,
        "toc": eph.get("toc", eph.get("toe", 0.0)),
        "af0": eph.get("af0", 0.0),
        "af1": eph.get("af1", 0.0),
        "af2": eph.get("af2", 0.0),
        "iodc": int(_f(eph.get("iode", 0))) & 0x3FF,
        "tgd_b2ap": eph.get("tgd_b2ap", eph.get("tgd", 0.0)),
        "isc_b2ad": eph.get("isc_b2ad", 0.0),
    }


_VALUES = {L.MSG_EPH1: _values_mt10, L.MSG_EPH2: _values_mt11,
           L.MSG_CLK_IONO: _values_mt30}


def encode_info_bits(mtype: int, eph: dict, prn: int, sow: float, week: int) -> list[int]:
    """288-bit info block (header + message fields + CRC-24Q) for one
    B-CNAV2 message type."""
    bits = [0] * 264
    header_vals = {"prn": int(prn), "mes_type": int(mtype),
                   "sow": int(round(sow)) % 604800}
    for name, off, n, kind, scale in L.HEADER:
        _write_field(bits, off, n, kind, scale, header_vals[name])
    if mtype == L.MSG_EPH1:
        values = _values_mt10(eph, prn, week)
    elif mtype == L.MSG_EPH2:
        values = _values_mt11(eph)
    elif mtype == L.MSG_CLK_IONO:
        values = _values_mt30(eph)
    else:
        raise ValueError(f"unsupported B-CNAV2 message type {mtype}")
    for name, off, n, kind, scale in L.FIELDS_BY_TYPE[mtype]:
        _write_field(bits, off, n, kind, scale, values.get(name, 0))
    crc = crc24q(bits[:_DATA_BITS])
    return bits + crc


def build_frame(mtype: int, eph: dict, prn: int, sow: float, week: int) -> list[int]:
    """One 600-symbol B-CNAV2 frame: 24-symbol preamble + LDPC(576,288)."""
    info = encode_info_bits(mtype, eph, prn, sow, week)
    coded = ldpc.encode(info)
    frame = _PREAMBLE + coded
    assert len(frame) == 600, len(frame)
    return frame


# 9 s cycle: MT10, MT11 (must immediately follow MT10 per the real
# receiver's cross-frame linkage), MT30 (clock + B2a group delay).
_CYCLE = (L.MSG_EPH1, L.MSG_EPH2, L.MSG_CLK_IONO)


def nav_stream(eph: dict, prn: int, week: int, tow0_sow: float,
               duration_s: float, eph_by_prn=None) -> tuple[np.ndarray, float]:
    """Deterministic B-CNAV2 symbol stream, int8 in {-1, +1} at 200 sym/s.
    Length covers duration_s rounded up to a whole 3 s frame plus a 30 s
    guard, aligned so a frame boundary sits on the 3 s SOW grid."""
    nframes = int(math.ceil((math.ceil(duration_s) + 30) / 3.0))
    tow0 = int(round(tow0_sow))
    tow0 -= tow0 % 3
    syms: list[int] = []
    for f in range(nframes):
        sow = (tow0 + 3 * f) % 604800
        mtype = _CYCLE[f % len(_CYCLE)]
        syms.extend(build_frame(mtype, eph, prn, sow, week))
    bits = np.asarray(syms, dtype=np.int8)
    arr = np.where(bits > 0, np.int8(-1), np.int8(1))   # bit=0->+1, bit=1->-1
    return arr, 200.0
