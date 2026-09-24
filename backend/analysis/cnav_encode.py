"""GPS CNAV encoder (IS-GPS-200 Sections 30 / 40) -- the L2C and L5
navigation message. 300-bit messages, one per 12 s, rate-1/2 K=7
convolutional coding (G1 = 0o171, G2 = 0o133 inverted), no interleaving,
CRC-24Q over bits 1..276 -> 50 symbols/s. Message types 10, 11
(ephemeris), 30 (clock + iono + group delay), 33 (clock + UTC), cycled.

Field bit-allocation is driven by the shared table in
``backend.analysis._cnav_layout`` so the decoder stays in lockstep.
"""
from __future__ import annotations

import math

import numpy as np

from backend.analysis import _cnav_layout as L
from backend.analysis._crc import crc24q

_PREAMBLE = [1, 0, 0, 0, 1, 0, 1, 1]        # 0x8B
_G1 = 0o171
_G2 = 0o133
_SEMI = 1.0 / math.pi                        # radians -> semicircles


def _f(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if (math.isnan(v) or math.isinf(v)) else v


def bits_of(value: int, n: int) -> list[int]:
    value = int(value) & ((1 << n) - 1)
    return [(value >> (n - 1 - i)) & 1 for i in range(n)]


def _enc_field(kind: str, scale: float, nbits: int, phys: float) -> list[int]:
    q = int(round(_f(phys) / scale))
    if kind == "s":
        lo, hi = -(1 << (nbits - 1)), (1 << (nbits - 1)) - 1
        q = max(lo, min(hi, q))
        q &= (1 << nbits) - 1
    else:
        q = max(0, min((1 << nbits) - 1, q))
    return bits_of(q, nbits)


def _parity(x: int) -> int:
    return bin(x).count("1") & 1


class _Conv:
    """Continuous rate-1/2 K=7 convolutional coder, G2 inverted. State
    persists across ``encode`` calls (one coder per navigation stream)."""

    def __init__(self) -> None:
        self.reg = 0

    def encode(self, data) -> list[int]:
        out: list[int] = []
        for b in data:
            self.reg = ((self.reg << 1) | (int(b) & 1)) & 0x7F
            out.append(_parity(self.reg & _G1))
            out.append(_parity(self.reg & _G2) ^ 1)
        return out


# --- physical-value resolvers: eph/header dict -> {field name: value} -----

def _resolve_10(eph: dict, hdr: dict) -> dict:
    A = _f(eph.get("sqrtA", 0.0)) ** 2
    return {
        "wn": eph.get("gps_week", 0),
        "health": 0,
        "t_op": eph.get("toe", 0.0),
        "ura_ed": 0,
        "toe": eph.get("toe", 0.0),
        "dA": A - L.A_REF,
        "A_dot": 0.0,
        "delta_n": _f(eph.get("delta_n", 0.0)) * _SEMI,
        "delta_n_dot": 0.0,
        "m0": _f(eph.get("m0", 0.0)) * _SEMI,
        "e": eph.get("e", 0.0),
        "omega": _f(eph.get("omega", 0.0)) * _SEMI,
    }


def _resolve_11(eph: dict, hdr: dict) -> dict:
    return {
        "toe": eph.get("toe", 0.0),
        "omega0": _f(eph.get("omega0", 0.0)) * _SEMI,
        "i0": _f(eph.get("i0", 0.0)) * _SEMI,
        "d_omega_dot": _f(eph.get("omega_dot", 0.0)) * _SEMI - L.OMEGA_DOT_REF,
        "idot": _f(eph.get("idot", 0.0)) * _SEMI,
        "cis": eph.get("cis", 0.0),
        "cic": eph.get("cic", 0.0),
        "crs": eph.get("crs", 0.0),
        "crc": eph.get("crc", 0.0),
        "cus": eph.get("cus", 0.0),
        "cuc": eph.get("cuc", 0.0),
    }


def _resolve_30(eph: dict, hdr: dict) -> dict:
    a = list(hdr.get("iono_alpha", [0, 0, 0, 0])) + [0, 0, 0, 0]
    b = list(hdr.get("iono_beta", [0, 0, 0, 0])) + [0, 0, 0, 0]
    return {
        "toc": eph.get("toc", eph.get("toe", 0.0)),
        "ura_ned0": 0, "ura_ned1": 0, "ura_ned2": 0,
        "af0": eph.get("af0", 0.0),
        "af1": eph.get("af1", 0.0),
        "af2": eph.get("af2", 0.0),
        "tgd": eph.get("tgd", 0.0),
        "isc_l1ca": eph.get("isc_l1ca", 0.0),
        "isc_l2c": eph.get("isc_l2c", 0.0),
        "isc_l5i5": eph.get("isc_l5i5", 0.0),
        "isc_l5q5": eph.get("isc_l5q5", 0.0),
        "a0": a[0], "a1": a[1], "a2": a[2], "a3": a[3],
        "b0": b[0], "b1": b[1], "b2": b[2], "b3": b[3],
    }


def _resolve_33(eph: dict, hdr: dict) -> dict:
    u = hdr.get("utc", {}) or {}
    return {
        "utc_a0": u.get("a0", 0.0),
        "utc_a1": u.get("a1", 0.0),
        "utc_a2": 0.0,
        "dt_ls": u.get("dt_ls", 18),
        "t_ot": u.get("t_ot", 0),
        "wn_ot": u.get("wn_ot", eph.get("gps_week", 0)),
        "wn_lsf": u.get("wn_lsf", eph.get("gps_week", 0)),
        "dn": u.get("dn", 0),
        "dt_lsf": u.get("dt_lsf", 18),
    }


_RESOLVE = {10: _resolve_10, 11: _resolve_11, 30: _resolve_30, 33: _resolve_33}


def _payload(msg_type: int, eph: dict, hdr: dict) -> list[int]:
    values = _RESOLVE[msg_type](eph or {}, hdr or {})
    bits: list[int] = []
    for name, kind, scale, nbits in L.LAYOUT[msg_type]:
        bits += _enc_field(kind, scale, nbits, values[name])
    if len(bits) > L.PAYLOAD_BITS:
        raise ValueError(f"CNAV type {msg_type} payload {len(bits)} > "
                         f"{L.PAYLOAD_BITS} bits")
    return bits + [0] * (L.PAYLOAD_BITS - len(bits))


def build_message(msg_type: int, prn: int, tow_6s: int, alert: int,
                  eph: dict, header: dict) -> list[int]:
    body = _PREAMBLE + bits_of(int(prn), 6) + bits_of(int(msg_type), 6)
    body += bits_of(int(tow_6s), 17) + [int(alert) & 1]
    body += _payload(msg_type, eph, header)
    assert len(body) == L.MSG_BITS - L.CRC_BITS, len(body)
    return body + crc24q(body)


def nav_stream(eph, header, week, sow, duration_s, *, prn, eph_by_prn=None):
    """(+/-1 int8 symbols at 50 sym/s, 50.0). Cycles message types
    [10, 11, 30, 33], one 300-bit message per 12 s, TOW field carrying the
    start of the following message (6 s units)."""
    n_msg = max(1, math.ceil((math.ceil(float(duration_s)) + 24) / 12))
    # Messages sit on the 12 s GPS-time grid at or before ``sow`` (the
    # engine passes sow minus a margin so symbol 0 precedes the earliest
    # transmit time). ceil() here used to start the stream AFTER sow.
    tow0 = int(float(sow) // 12.0) * 2
    eph = dict(eph or {})
    eph.setdefault("gps_week", int(week))
    conv = _Conv()
    bits: list[int] = []
    for i in range(n_msg):
        # Message type follows GPS time (message count since the week
        # start), so a stream started later continues the same schedule.
        mtype = L.CYCLE[(tow0 // 2 + i) % len(L.CYCLE)]
        tow = tow0 + (i + 1) * 2          # next message start, 12 s = 2 units
        bits.extend(conv.encode(build_message(mtype, prn, tow, 0, eph, header)))
    arr = np.array([1 if s == 0 else -1 for s in bits], dtype=np.int8)
    return arr, 50.0
