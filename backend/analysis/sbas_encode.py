"""SP-D: SBAS L1 (WAAS/EGNOS-style) navigation message encoder.

RTCA DO-229 L1 signal:

* 250 bps data, rate-1/2 constraint-length-7 convolutional FEC
  (G1 = 0o171, G2 = 0o133, G2 **not** inverted), encoder state carried
  continuously across blocks (no per-block reset, no tail) -> 500 sym/s.
* 250-bit block = 8-bit preamble + 6-bit message type + 212-bit data
  + 24-bit CRC-24Q (over the preceding 226 bits). No interleaving.
* The 8-bit preamble cycles ``0x53, 0x9A, 0xC6`` over three consecutive
  blocks (the 24-bit unique word).

Emits message type 9 (GEO navigation message) carrying the broadcast
record's ECEF position / velocity / acceleration, laid contiguously into
the 212-bit data field with a matching :func:`decode_stream` for the
round-trip test. Other blocks are message type 63 (null) with a valid CRC.
"""
from __future__ import annotations

import math

import numpy as np

SYM_RATE_HZ = 500.0
_PREAMBLES = (0x53, 0x9A, 0xC6)
_CRC24Q = 0x1864CFB
_G1 = 0o171
_G2 = 0o133

_POS = 0.08          # metres  (DO-229 GEO position LSB)
_VEL = 0.000625      # m/s
_ACC = 0.0000050     # m/s^2


def _f(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(v) or math.isinf(v) else v


def bits_of(value: int, n: int) -> list[int]:
    v = int(value) & ((1 << n) - 1)
    return [(v >> (n - 1 - i)) & 1 for i in range(n)]


def twos(value, scale, n) -> list[int]:
    q = int(round(_f(value) / scale))
    lo, hi = -(1 << (n - 1)), (1 << (n - 1)) - 1
    return bits_of(max(lo, min(hi, q)), n)


def crc24q(bits: list[int]) -> list[int]:
    reg = 0
    for b in bits:
        reg ^= (int(b) & 1) << 23
        reg <<= 1
        if reg & (1 << 24):
            reg ^= _CRC24Q
        reg &= 0xFFFFFF
    for _ in range(24):
        reg <<= 1
        if reg & (1 << 24):
            reg ^= _CRC24Q
        reg &= 0xFFFFFF
    return bits_of(reg, 24)


# --------------------------------------------------------------------------
# rate-1/2 K=7 convolutional, continuous state
# --------------------------------------------------------------------------
def _parity(x: int) -> int:
    return bin(x).count("1") & 1


class _Conv:
    def __init__(self):
        self.reg = 0

    def encode(self, data: list[int]) -> list[int]:
        out = []
        for b in data:
            self.reg = ((self.reg << 1) | (int(b) & 1)) & 0x7F
            out.append(_parity(self.reg & _G1))
            out.append(_parity(self.reg & _G2))
        return out


def conv_decode_stream(coded: list[int]) -> list[int]:
    reg = 0
    out = []
    for i in range(0, len(coded) - 1, 2):
        for cand in (0, 1):
            r = ((reg << 1) | cand) & 0x7F
            if (_parity(r & _G1) == coded[i]
                    and _parity(r & _G2) == coded[i + 1]):
                out.append(cand)
                reg = r
                break
        else:                                   # pragma: no cover - clean only
            out.append(0)
            reg = (reg << 1) & 0x7F
    return out


# --------------------------------------------------------------------------
# blocks
# --------------------------------------------------------------------------
def _type9_data(eph: dict) -> list[int]:
    b: list[int] = []
    b += bits_of(0, 8)                                   # IODN / spare
    b += twos(eph.get("x_m"), _POS, 30)
    b += twos(eph.get("y_m"), _POS, 30)
    b += twos(eph.get("z_m"), _POS, 25)
    b += twos(eph.get("vx"), _VEL, 17)
    b += twos(eph.get("vy"), _VEL, 17)
    b += twos(eph.get("vz"), _VEL, 18)
    b += twos(eph.get("ax"), _ACC, 10)
    b += twos(eph.get("ay"), _ACC, 10)
    b += twos(eph.get("az"), _ACC, 10)
    b = (b + [0] * 212)[:212]
    return b


def build_block(idx: int, mtype: int, eph: dict) -> list[int]:
    pre = bits_of(_PREAMBLES[idx % 3], 8)
    data = _type9_data(eph) if mtype == 9 else [0] * 212
    head = pre + bits_of(mtype, 6) + data                # 226 bits
    return head + crc24q(head)                           # 250 bits


def nav_stream(eph: dict, week: int, tow0_sow: float, duration_s: float,
               eph_by_prn=None) -> tuple[np.ndarray, float]:
    """Deterministic SBAS L1 symbol stream, ``int8`` {-1,+1} at 500 sym/s
    (250 bps data through the continuous rate-1/2 FEC)."""
    nblk = int(math.ceil((math.ceil(duration_s) + 30)))   # 1 block/s
    conv = _Conv()
    syms: list[int] = []
    # Block k is the one sent during GPS second k of the week: the preamble
    # rotation (0x53 on a 6 s GPS epoch, RTCA DO-229) and the message-type
    # schedule follow GPS time, not the stream start -- a stream started
    # later (live mode restarts it every segment) continues the sequence.
    k0 = int(math.floor(float(tow0_sow)))
    for i in range(nblk):
        k = k0 + i
        mtype = 9 if (k % 2 == 0) else 63
        syms.extend(conv.encode(build_block(k, mtype, eph)))
    a = np.asarray(syms, dtype=np.int8)
    return np.where(a > 0, np.int8(1), np.int8(-1)).astype(np.int8), SYM_RATE_HZ


def decode_stream(stream: np.ndarray) -> list[dict]:
    """Recover the message blocks from a clean stream (round-trip test)."""
    coded = (np.asarray(stream) > 0).astype(int).tolist()
    data = conv_decode_stream(coded)
    out = []
    for i in range(len(data) // 250):
        blk = data[i * 250:(i + 1) * 250]
        head, crc = blk[:226], blk[226:]
        ok = crc24q(head) == crc
        mtype = int("".join(map(str, blk[8:14])), 2)
        out.append({"preamble": int("".join(map(str, blk[:8])), 2),
                    "type": mtype, "crc_ok": ok, "data": blk[14:226]})
    return out
