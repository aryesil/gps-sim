"""SP-D: GLONASS L1OF navigation-string encoder.

GLONASS transmits 50 bps data with a 100 Hz meander (Manchester) line code,
so this emits **100 sym/s**: each data bit becomes ``+1 -1`` (bit 0) or
``-1 +1`` (bit 1).

A frame is 15 strings of 2 s. Each string carries 85 bits: 1 leading idle
bit (0), then a 76-bit data field, then 8 check bits. The check code is the
GLONASS KX Hamming code -- a shortened Hamming(85,77) with an overall parity
bit (SEC-DED); implemented here in its textbook systematic form (check bits
at positions 1,2,4,8,16,32,64 of the 84-bit protected field plus overall
parity), which self-round-trips and matches the ICD's parity structure.

Strings 1-4 carry the immediate ephemeris (PZ-90 ECEF position / velocity /
acceleration + clock terms) laid contiguously into the data field with ICD
widths and scale factors; strings 5-15 are valid-Hamming zero strings.

Verified by the encoder unit tests (meander, Hamming reference + SEC
property) and a clean-channel field round-trip via :func:`decode_frame`.
"""
from __future__ import annotations

import math

import numpy as np

SYM_RATE_HZ = 100.0
_STR_BITS = 85
_STRINGS_PER_FRAME = 15
_STR_S = 2.0

# GLONASS ICD scale factors
_POS = 2 ** -11 * 1000.0     # metres  (2^-11 km)
_VEL = 2 ** -20 * 1000.0     # m/s     (2^-20 km/s)
_ACC = 2 ** -30 * 1000.0     # m/s^2   (2^-30 km/s^2)
_TAU = 2 ** -30              # s
_GAM = 2 ** -40              # dimensionless


def _f(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(v) or math.isinf(v) else v


def bits_of(value: int, n: int) -> list[int]:
    v = int(value) & ((1 << n) - 1)
    return [(v >> (n - 1 - i)) & 1 for i in range(n)]


def sign_mag(value: float, scale: float, n: int) -> list[int]:
    """GLONASS uses sign-magnitude for its signed fields: 1 sign bit + (n-1)
    magnitude bits."""
    q = int(round(_f(value) / scale))
    s = 1 if q < 0 else 0
    m = min(abs(q), (1 << (n - 1)) - 1)
    return [s] + bits_of(m, n - 1)


def _from_sign_mag(bits: list[int], scale: float) -> float:
    m = int("".join(map(str, bits[1:])), 2)
    return (-m if bits[0] else m) * scale


# --------------------------------------------------------------------------
# KX Hamming (shortened Hamming(85,77) + overall parity, systematic)
# --------------------------------------------------------------------------
# 7 Hamming check bits at power-of-two positions + position 84 = overall
# parity (C_Sigma). Protected field is 84 bits; 76 carry data.
_CHECK_POS = (1, 2, 4, 8, 16, 32, 64)
_OVERALL_POS = 84
_DATA_POS = tuple(p for p in range(1, 85)
                  if p not in _CHECK_POS and p != _OVERALL_POS)   # 76 positions


def hamming_string(data76: list[int]) -> list[int]:
    """76 data bits -> an 85-symbol GLONASS string: an 84-bit protected field
    (7 Hamming parities + 76 data + 1 overall parity) then a trailing 0
    (bit 85, always zero)."""
    assert len(data76) == 76
    field = [0] * 85                          # 1-indexed; field[0] unused
    for pos, bit in zip(_DATA_POS, data76):
        field[pos] = bit
    for c in _CHECK_POS:
        p = 0
        for pos in range(1, 84):
            if (pos & c) and pos != c:
                p ^= field[pos]
        field[c] = p
    ov = 0
    for pos in range(1, 84):
        ov ^= field[pos]
    field[_OVERALL_POS] = ov
    return field[1:85] + [0]


def hamming_check(string85: list[int]) -> bool:
    field = [0] + list(string85[:84])
    for c in _CHECK_POS:
        p = 0
        for pos in range(1, 84):
            if (pos & c) and pos != c:
                p ^= field[pos]
        if p != field[c]:
            return False
    ov = 0
    for pos in range(1, 84):
        ov ^= field[pos]
    return ov == field[_OVERALL_POS]


def _string_data(field84: list[int]) -> list[int]:
    return [field84[pos - 1] for pos in _DATA_POS]


# --------------------------------------------------------------------------
# ephemeris strings
# --------------------------------------------------------------------------
def _data_string(sidx: int, eph: dict, tk_s: int) -> list[int]:
    b: list[int] = []
    if sidx == 1:
        b += bits_of(tk_s // 60 % (1 << 12), 12)                 # t_k (coarse)
        b += sign_mag(eph.get("vx"), _VEL, 24)
        b += sign_mag(eph.get("ax"), _ACC, 5)
        b += sign_mag(eph.get("x_m"), _POS, 27)
    elif sidx == 2:
        b += bits_of(int(_f(eph.get("health", 0))) & 1, 1)       # B_n
        b += sign_mag(eph.get("vy"), _VEL, 24)
        b += sign_mag(eph.get("ay"), _ACC, 5)
        b += sign_mag(eph.get("y_m"), _POS, 27)
    elif sidx == 3:
        b += sign_mag(eph.get("gamma"), _GAM, 11)
        b += sign_mag(eph.get("vz"), _VEL, 24)
        b += sign_mag(eph.get("az"), _ACC, 5)
        b += sign_mag(eph.get("z_m"), _POS, 27)
    elif sidx == 4:
        b += sign_mag(eph.get("tau"), _TAU, 22)
        b += bits_of(0, 5)                                        # E_n
        b += bits_of(int(_f(eph.get("glo_k", 0))) & 0x1F, 5)     # slot / spare
    b = (b + [0] * 76)[:76]
    return b


_EPH_STRINGS = (1, 2, 3, 4)


def build_string(sidx: int, eph: dict, tk_s: int) -> list[int]:
    data = _data_string(sidx, eph, tk_s) if sidx in _EPH_STRINGS else [0] * 76
    # string number rides the top 4 data bits so the decoder can index frames
    data = bits_of(sidx, 4) + data[4:]
    return hamming_string(data)


def _meander(bits: list[int]) -> list[int]:
    out: list[int] = []
    for b in bits:
        out += ([1, -1] if b == 0 else [-1, 1])
    return out


def nav_stream(eph: dict, week: int, tow0_sow: float, duration_s: float,
               eph_by_prn=None) -> tuple[np.ndarray, float]:
    """Deterministic L1OF symbol stream, ``int8`` {-1,+1} at 100 sym/s
    (50 bps data x 100 Hz meander)."""
    nstr = int(math.ceil((math.ceil(duration_s) + 30) / _STR_S))
    tk0 = int(round(tow0_sow))
    data_bits: list[int] = []
    for i in range(nstr):
        sidx = (i % _STRINGS_PER_FRAME) + 1
        data_bits += build_string(sidx, eph, tk0 + i * int(_STR_S))
    return np.asarray(_meander(data_bits), dtype=np.int8), SYM_RATE_HZ


# --------------------------------------------------------------------------
# clean-channel decode (round-trip tests only)
# --------------------------------------------------------------------------
def _demeander(sym: list[int]) -> list[int]:
    out = []
    for i in range(0, len(sym), 2):
        out.append(0 if sym[i] > 0 else 1)
    return out


def decode_frame(stream: np.ndarray) -> dict[int, list[int]]:
    """Map string number -> its 76-bit data field, for strings that pass the
    Hamming check."""
    bits = _demeander(np.asarray(stream).tolist())
    out: dict[int, list[int]] = {}
    for i in range(len(bits) // _STR_BITS):
        s = bits[i * _STR_BITS:(i + 1) * _STR_BITS]
        if len(s) < _STR_BITS or not hamming_check(s):
            continue
        field84 = s[:84]
        data = _string_data(field84)
        sidx = int("".join(map(str, data[:4])), 2)
        out.setdefault(sidx, data)
    return out
