"""SP-D: GLONASS L1OF/L2OF navigation-string encoder (GLONASS ICD 5.1).

A string is 2 s: 85 data bits (1.7 s: relative code, then a 100 Hz meander,
so 170 symbols) followed by the 0.3 s time mark (30 chips of 10 ms), i.e.
200 symbols at **100 sym/s**. A frame is 15 strings (30 s), five frames make
a superframe (2.5 min); frames start every 30 s of Moscow time (UTC + 3 h),
so the string number, t_k and the frame counter follow GLONASS time, never
the stream start.

String bits are numbered 85..1 in transmission order: bit 85 is the idle
bit (0), bits 84..81 the string number m, bits 8..1 the KX Hamming check
bits (beta_8..beta_1). Field positions below are 1-based transmission
positions (position 1 = bit 85), the convention GNSS-SDR's GLONASS_L1_L2_CA
tables use; the KX index sets are the ICD's.

Strings 1-4 carry the immediate ephemeris (PZ-90 ECEF state + clock), string
5 the time parameters (N_A, tau_c, N_4, tau_GPS); the almanac strings 6-15
carry only their string number (C_n = 0: no almanac satellite usable).

Sign convention: records hold RINEX's GLONASS clock bias, which is -TauN;
the broadcast tau_n field is the ICD TauN, so it is written negated.
"""
from __future__ import annotations

import datetime as _dt
import math

import numpy as np

SYM_RATE_HZ = 100.0
_STR_BITS = 85
_STRINGS_PER_FRAME = 15
_STR_S = 2.0
_STR_SYMS = 200                       # 170 data symbols + 30 time-mark chips
_TIME_MARK = [int(c) for c in "111110001101110101000010010110"]
_MSK_MINUS_UTC_S = 10800

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
# KX Hamming code (ICD 5.1 Sec. 4.7): check bits beta_1..beta_7 over the
# listed ICD bit numbers, beta_8 the overall parity of bits 1..85.
# --------------------------------------------------------------------------
_KX_SETS = (
    (9, 10, 12, 13, 15, 17, 19, 20, 22, 24, 26, 28, 30, 32, 34, 35, 37, 39,
     41, 43, 45, 47, 49, 51, 53, 55, 57, 59, 61, 63, 65, 66, 68, 70, 72, 74,
     76, 78, 80, 82, 84),
    (9, 11, 12, 14, 15, 18, 19, 21, 22, 25, 26, 29, 30, 33, 34, 36, 37, 40,
     41, 44, 45, 48, 49, 52, 53, 56, 57, 60, 61, 64, 65, 67, 68, 71, 72, 75,
     76, 79, 80, 83, 84),
    (10, 11, 12, 16, 17, 18, 19, 23, 24, 25, 26, 31, 32, 33, 34, 38, 39, 40,
     41, 46, 47, 48, 49, 54, 55, 56, 57, 62, 63, 64, 65, 69, 70, 71, 72, 77,
     78, 79, 80, 85),
    (13, 14, 15, 16, 17, 18, 19, 27, 28, 29, 30, 31, 32, 33, 34, 42, 43, 44,
     45, 46, 47, 48, 49, 58, 59, 60, 61, 62, 63, 64, 65, 73, 74, 75, 76, 77,
     78, 79, 80),
    tuple(range(20, 35)) + tuple(range(50, 66)) + (81, 82, 83, 84, 85),
    tuple(range(35, 66)),
    tuple(range(66, 86)),
)


def _icd_bit(s85, n: int) -> int:
    """ICD bit number n (85..1) of a string in transmission order."""
    return s85[85 - n]


def hamming_string(data76: list[int]) -> list[int]:
    """76 data bits (ICD bits 84..9: string number then payload) -> the
    85-bit string in transmission order: idle 0, data, beta_8..beta_1."""
    assert len(data76) == 76
    s = [0] + list(data76) + [0] * 8
    beta = [0] * 9
    for k, idx in enumerate(_KX_SETS, start=1):
        p = 0
        for n in idx:
            p ^= _icd_bit(s, n)
        beta[k] = p
    ov = 0
    for n in range(9, 86):
        ov ^= _icd_bit(s, n)
    for k in range(1, 8):
        ov ^= beta[k]
    beta[8] = ov
    for k in range(1, 9):
        s[85 - k] = beta[k]
    return s


def hamming_check(string85: list[int]) -> bool:
    s = list(string85)
    if len(s) < 85:
        return False
    for k, idx in enumerate(_KX_SETS, start=1):
        p = _icd_bit(s, k)
        for n in idx:
            p ^= _icd_bit(s, n)
        if p:
            return False
    ov = 0
    for n in range(1, 86):
        ov ^= _icd_bit(s, n)
    return ov == 0


def _string_data(field84: list[int]) -> list[int]:
    """The 76 data bits (string number + payload) of a string."""
    return list(field84[1:77])


# --------------------------------------------------------------------------
# strings
# --------------------------------------------------------------------------
def _put(s: list[int], pos: int, bits: list[int]) -> None:
    s[pos - 1:pos - 1 + len(bits)] = bits


def _get_u(s, pos: int, n: int) -> int:
    return int("".join(map(str, s[pos - 1:pos - 1 + n])), 2)


def _get_sm(s, pos: int, n: int, scale: float) -> float:
    return _from_sign_mag(list(s[pos - 1:pos - 1 + n]), scale)


def _glonass_time(week: int, gps_sow: float):
    """(Moscow date, seconds of the Moscow day) for a GPS week/SoW."""
    from backend import config
    gps = _dt.datetime(1980, 1, 6) + _dt.timedelta(weeks=int(week),
                                                    seconds=float(gps_sow))
    msk = gps - _dt.timedelta(seconds=config.GPS_UTC_LEAP_S) \
        + _dt.timedelta(seconds=_MSK_MINUS_UTC_S)
    return msk.date(), (msk - _dt.datetime(msk.year, msk.month, msk.day)).total_seconds()


def _nt_n4(date: _dt.date) -> tuple[int, int]:
    """Day number within the four-year interval, and that interval's
    number since 1996 (ICD N_T / N_4)."""
    n4 = (date.year - 1996) // 4 + 1
    nt = (date - _dt.date(1996 + 4 * (n4 - 1), 1, 1)).days + 1
    return nt, n4


def _tb_index(eph: dict) -> int:
    """t_b: the record's reference epoch as 15-min index of the Moscow
    day. ``toe_ref`` is GPS seconds of week (see ephemeris.align_epochs)."""
    from backend import config
    t = _f(eph.get("toe_ref", 0.0)) - config.GPS_UTC_LEAP_S + _MSK_MINUS_UTC_S
    return int(round((t % 86400.0) / 900.0)) % 96


def build_string(sidx: int, eph: dict, week: int, gps_sow: float) -> list[int]:
    """String number ``sidx`` (1..15) as sent in the 2 s slot starting at
    GPS ``week``/``gps_sow``: 85 bits in transmission order."""
    s = [0] * 85
    _put(s, 2, bits_of(sidx, 4))
    date, tod = _glonass_time(week, gps_sow)
    tf = int(tod) - int(tod) % 30                     # frame start, s of day
    tb = _tb_index(eph)
    nt, n4 = _nt_n4(date)
    if sidx == 1:
        _put(s, 8, bits_of(0b01, 2))                  # P1: 30 min t_b spacing
        _put(s, 10, bits_of(tf // 3600, 5))           # t_k hours
        _put(s, 15, bits_of((tf % 3600) // 60, 6))    # t_k minutes
        _put(s, 21, [(tf % 60) // 30])                # t_k 30 s
        _put(s, 22, sign_mag(eph.get("vx"), _VEL, 24))
        _put(s, 46, sign_mag(eph.get("ax"), _ACC, 5))
        _put(s, 51, sign_mag(eph.get("x_m"), _POS, 27))
    elif sidx == 2:
        _put(s, 6, bits_of((int(_f(eph.get("health", 0))) & 1) << 2, 3))  # B_n
        _put(s, 9, [tb & 1])                          # P2
        _put(s, 10, bits_of(tb, 7))
        _put(s, 22, sign_mag(eph.get("vy"), _VEL, 24))
        _put(s, 46, sign_mag(eph.get("ay"), _ACC, 5))
        _put(s, 51, sign_mag(eph.get("y_m"), _POS, 27))
    elif sidx == 3:
        _put(s, 6, [1])                               # P3: 5 almanacs/frame
        _put(s, 7, sign_mag(eph.get("gamma"), _GAM, 11))
        _put(s, 22, sign_mag(eph.get("vz"), _VEL, 24))
        _put(s, 46, sign_mag(eph.get("az"), _ACC, 5))
        _put(s, 51, sign_mag(eph.get("z_m"), _POS, 27))
    elif sidx == 4:
        _put(s, 6, sign_mag(-_f(eph.get("tau")), _TAU, 22))   # TauN = -bias
        _put(s, 60, bits_of(nt, 11))                  # N_T
        _put(s, 71, bits_of(int(_f(eph.get("prn", 0))) & 0x1F, 5))  # slot n
        _put(s, 76, bits_of(0b01, 2))                 # M: GLONASS-M
    elif sidx == 5:
        _put(s, 6, bits_of(nt, 11))                   # N_A
        _put(s, 50, bits_of(n4, 5))                   # N_4
    return hamming_string(s[1:77])


def _meander(bits: list[int]) -> list[int]:
    out: list[int] = []
    for b in bits:
        out += ([1, -1] if b == 0 else [-1, 1])
    return out


def _relative(bits: list[int]) -> list[int]:
    """ICD relative code: each transmitted bit is the data bit XOR the
    previously transmitted one (receivers decode it differentially, which
    also removes the carrier-phase sign ambiguity)."""
    out, prev = [], 0
    for b in bits:
        prev ^= b
        out.append(prev)
    return out


def string_symbols(s85: list[int]) -> list[int]:
    """One 2 s string: relative code + meander (170), then the time mark."""
    return (_meander(_relative(s85))
            + [(-1 if c else 1) for c in _TIME_MARK])


def nav_stream(eph: dict, week: int, tow0_sow: float, duration_s: float,
               eph_by_prn=None) -> tuple[np.ndarray, float]:
    """Deterministic L1OF/L2OF symbol stream, ``int8`` {-1,+1} at 100
    sym/s, starting at the 2 s string boundary at or before ``tow0_sow``
    (Moscow even seconds coincide with GPS even seconds: the leap offset
    and the 3 h are both even). Strings follow GLONASS time, so a stream
    started later carries the same symbols at the same instants."""
    nstr = int(math.ceil((math.ceil(duration_s) + 30) / _STR_S))
    t0 = int(float(tow0_sow) // 2) * 2
    syms: list[int] = []
    for i in range(nstr):
        t = t0 + 2 * i
        _date, tod = _glonass_time(week, t)
        sidx = (int(tod) // 2) % _STRINGS_PER_FRAME + 1
        syms += string_symbols(build_string(sidx, eph, week, t))
    return np.asarray(syms, dtype=np.int8), SYM_RATE_HZ


# --------------------------------------------------------------------------
# clean-channel decode (round-trip tests only)
# --------------------------------------------------------------------------
def _derelative(rel: list[int]) -> list[int]:
    """Undo the relative code within one string (bit 85 is idle 0)."""
    return [0] + [rel[j] ^ rel[j - 1] for j in range(1, len(rel))]


def decode_frame(stream: np.ndarray) -> dict[int, list[int]]:
    """Map string number -> its 76 data bits for strings that pass KX,
    from a clean stream that starts on a string boundary."""
    sym = np.asarray(stream).tolist()
    out: dict[int, list[int]] = {}
    for i in range(len(sym) // _STR_SYMS):
        blk = sym[i * _STR_SYMS:i * _STR_SYMS + 170]
        rel = [0 if blk[2 * j] > 0 else 1 for j in range(85)]
        s = _derelative(rel)
        if not hamming_check(s):
            continue
        data = _string_data(s)
        out.setdefault(int("".join(map(str, data[:4])), 2), data)
    return out


def toe_ref_from_tb(tb: int, approx_gps_sow: float) -> float:
    """GPS SoW of broadcast t_b (15 min index of the Moscow day), taking the
    occurrence nearest ``approx_gps_sow``."""
    from backend import config
    tod = (float(approx_gps_sow) - config.GPS_UTC_LEAP_S + _MSK_MINUS_UTC_S) % 86400.0
    d = (tb * 900.0 - tod + 43200.0) % 86400.0 - 43200.0
    return float(approx_gps_sow) + d


def reconstruct_ephemeris(decoded: dict[int, list[int]], toe_ref: float,
                           glo_k: int | None = None) -> dict:
    """Turn ``decode_frame``'s output (string number -> 76 data bits) back
    into a record shaped like :func:`backend.synth.glonass.glonass_state`
    expects (``x_m,y_m,z_m,vx,vy,vz,ax,ay,az,tau,gamma,toe_ref``, plus
    ``health``/``slot``/``glo_k``). ``tau`` is returned in the record's
    RINEX convention (-TauN). ``toe_ref`` is supplied by the caller (the
    receiver already knows its epoch); ``glo_k`` (the FDMA channel) comes
    from acquisition -- the immediate strings do not carry it."""
    out: dict = {"system": "R", "toe_ref": float(toe_ref)}
    if glo_k is not None:
        out["glo_k"] = int(glo_k)

    def full(d):
        return [0] + list(d) + [0] * 8

    if 1 in decoded:
        s = full(decoded[1])
        out["vx"] = _get_sm(s, 22, 24, _VEL)
        out["ax"] = _get_sm(s, 46, 5, _ACC)
        out["x_m"] = _get_sm(s, 51, 27, _POS)
        out["tk_s"] = (_get_u(s, 10, 5) * 3600 + _get_u(s, 15, 6) * 60
                       + _get_u(s, 21, 1) * 30)
    if 2 in decoded:
        s = full(decoded[2])
        out["health"] = _get_u(s, 6, 1)
        out["tb"] = _get_u(s, 10, 7)
        out["vy"] = _get_sm(s, 22, 24, _VEL)
        out["ay"] = _get_sm(s, 46, 5, _ACC)
        out["y_m"] = _get_sm(s, 51, 27, _POS)
    if 3 in decoded:
        s = full(decoded[3])
        out["gamma"] = _get_sm(s, 7, 11, _GAM)
        out["vz"] = _get_sm(s, 22, 24, _VEL)
        out["az"] = _get_sm(s, 46, 5, _ACC)
        out["z_m"] = _get_sm(s, 51, 27, _POS)
    if 4 in decoded:
        s = full(decoded[4])
        out["tau"] = -_get_sm(s, 6, 22, _TAU)
        out["nt"] = _get_u(s, 60, 11)
        out["slot"] = _get_u(s, 71, 5)
    return out
