"""IS-GPS-200 LNAV bit assembly for the native engine.

Pure functions: no I/O. Everything downstream (``engine.py`` wiring, the
decoder round-trip) builds on the primitives here. Parity follows
IS-GPS-200 Table 20-XIV: the six parity bits are computed over the 24
*source* data bits plus the previous word's last two bits (D29*, D30*),
and the 24 transmitted data bits are the source bits XORed with D30*.
"""
from __future__ import annotations

import math as _math

import numpy as _np

from backend.analysis.utc_params import gps_utc_params

# IS-GPS-200 Table 20-XIV parity equations. 1-indexed source data bits.
_EQ = {
    25: (1, 2, 3, 5, 6, 10, 11, 12, 13, 14, 17, 18, 20, 23),
    26: (2, 3, 4, 6, 7, 11, 12, 13, 14, 15, 18, 19, 21, 24),
    27: (1, 3, 4, 5, 7, 8, 12, 13, 14, 15, 16, 19, 20, 22),
    28: (2, 4, 5, 6, 8, 9, 13, 14, 15, 16, 17, 20, 21, 23),
    29: (1, 3, 5, 6, 7, 9, 10, 14, 15, 16, 17, 18, 21, 22, 24),
    30: (3, 5, 6, 8, 9, 10, 11, 13, 15, 19, 22, 23, 24),
}
# Which previous-word bit each parity equation folds in.
_PREV = {25: "D29", 26: "D30", 27: "D29", 28: "D30", 29: "D30", 30: "D29"}

PREAMBLE = [1, 0, 0, 0, 1, 0, 1, 1]  # 0x8B


def bits_of(value: int, n: int) -> list[int]:
    """Big-endian bit list of ``value`` in ``n`` bits."""
    return [(value >> (n - 1 - i)) & 1 for i in range(n)]


def twos(value: float, scale: float, n: int) -> int:
    """Rounded two's-complement raw integer, masked to ``n`` bits."""
    raw = int(round(value / scale))
    if raw < 0:
        raw += 1 << n
    return raw & ((1 << n) - 1)


def parity_bits(source24, D29_prev: int, D30_prev: int) -> list[int]:
    """The six parity bits [D25..D30] for a word whose 24 source data bits
    are ``source24`` and whose previous word ended in (D29*, D30*)."""
    d = list(source24)
    if len(d) != 24:
        raise ValueError("source24 must be 24 bits")
    out = []
    for k in (25, 26, 27, 28, 29, 30):
        acc = D29_prev if _PREV[k] == "D29" else D30_prev
        for idx in _EQ[k]:
            acc ^= d[idx - 1]
        out.append(acc)
    return out


def make_word(source24, D29_prev: int, D30_prev: int) -> list[int]:
    """30-bit transmitted word: 24 data bits (XORed with D30*) + 6 parity."""
    d_src = list(source24)
    if len(d_src) != 24:
        raise ValueError("source24 must be 24 bits")
    p = parity_bits(d_src, D29_prev, D30_prev)
    d_tx = [b ^ D30_prev for b in d_src]
    return d_tx + p


def HOW_solve_bits(tow_count: int, subframe_id: int,
                   D29_prev: int, D30_prev: int) -> list[int]:
    """24 source bits for a HOW word with bits 23-24 chosen so the word's
    trailing computed parity (D29, D30) are both zero (IS-GPS-200 20.3.2)."""
    # bit 18 alert flag = 0; bit 19 anti-spoof flag = 1 (A-S mode ON, as every
    # operational GPS satellite has broadcast since the mid-1990s; a 0 here
    # says the satellite is not encrypting P(Y), which no real SV does).
    base = (bits_of(tow_count & 0x1FFFF, 17)
            + [0, 1] + bits_of(subframe_id & 0x7, 3))  # 22 bits
    for t23 in (0, 1):
        for t24 in (0, 1):
            src = base + [t23, t24]
            w = make_word(src, D29_prev, D30_prev)
            if w[28] == 0 and w[29] == 0:
                return src
    raise AssertionError("no HOW solve bits found")


# --- TLM / HOW / subframe skeleton --------------------------------------

# 14-bit TLM message of the frame being built: the GPS week mod 1024, which is
# what every public capture of real satellites carries (set by frame_bits).
_TLM_MSG = [0]


def tlm_word(D29_prev: int, D30_prev: int) -> list[int]:
    """Word 1: preamble + 14-bit TLM message + integrity + reserved."""
    src = PREAMBLE + bits_of(_TLM_MSG[0], 14) + [0, 0]
    return make_word(src, D29_prev, D30_prev)


def how_word(tow_count: int, subframe_id: int,
             D29_prev: int, D30_prev: int) -> list[int]:
    """Word 2: 17-bit TOW-count + alert/A-S + 3-bit subframe id, trailing
    parity solved to zero."""
    src = HOW_solve_bits(tow_count, subframe_id, D29_prev, D30_prev)
    return make_word(src, D29_prev, D30_prev)


def subframe(words_src, tow_count: int, subframe_id: int,
            D29_start: int = 0, D30_start: int = 0) -> list[int]:
    """Assemble a 300-bit subframe: TLM, HOW, then eight 24-bit source
    words (3..10), tracking D29*/D30* across word boundaries.

    ``tow_count`` is the count for *this* subframe; the HOW carries the
    count of the next subframe start (IS-GPS-200 20.3.3.2).
    """
    if len(words_src) != 8 or any(len(w) != 24 for w in words_src):
        raise ValueError("subframe needs 8 source words of 24 bits each")
    out: list[int] = []
    d29, d30 = D29_start, D30_start
    w1 = tlm_word(d29, d30)
    out += w1
    d29, d30 = w1[28], w1[29]
    w2 = how_word((tow_count + 1) & 0x1FFFF, subframe_id, d29, d30)
    out += w2
    d29, d30 = w2[28], w2[29]
    for k, src in enumerate(words_src):
        src = list(src)
        if k == len(words_src) - 1:
            # Word 10 ends in two non-information bits (bits 23-24, "t")
            # that IS-GPS-200 20.3.5.2 requires to be solved so the word's
            # D29/D30 parity bits are zero -- the next subframe's TLM is
            # built on D29* = D30* = 0. Leaving them at 0,0 made the TLM
            # parity (and, with D30* = 1, the preamble polarity) wrong for
            # a real receiver on roughly 3 of 4 subframes.
            src = _solve_trailing_zero_parity(src[:22], d29, d30)
        w = make_word(src, d29, d30)
        out += w
        d29, d30 = w[28], w[29]
    return out


def _solve_trailing_zero_parity(src22, D29_prev: int, D30_prev: int):
    for t23 in (0, 1):
        for t24 in (0, 1):
            src = list(src22) + [t23, t24]
            w = make_word(src, D29_prev, D30_prev)
            if w[28] == 0 and w[29] == 0:
                return src
    raise AssertionError("no word-10 solve bits found")


# --- Subframe 1: clock, health, URA, IODC, Tgd, Toc -------------------

def subframe1(eph: dict, week: int, tow_count: int) -> list[int]:
    """300 bits. Uses eph keys af0/af1/af2 (s, s/s, s/s^2), toc (s),
    tgd (s), iodc, health. URA index fixed at 0, CA/P-on-L2 = 01,
    L2-P-data flag = 0."""
    iodc = int(eph.get("iodc", 0)) & 0x3FF
    health = int(eph.get("health", 0)) & 0x3F
    w3 = (bits_of(week & 0x3FF, 10) + [0, 1] + bits_of(0, 4)
          + bits_of(health, 6) + bits_of(iodc >> 8, 2))
    # Reserved words 4-7 as satellites broadcast them (identical on every SV in
    # public captures; the values change with the control segment's uploads, so
    # this is the 2026-03 pattern).
    w4 = bits_of(0x345273, 24)
    w5 = bits_of(0x1A3A17, 24)
    w6 = bits_of(0x45FA4B, 24)
    w7 = bits_of(0x8D36, 16) + bits_of(twos(eph["tgd"], 2 ** -31, 8), 8)
    w8 = bits_of(iodc & 0xFF, 8) + bits_of(round(eph["toc"] / 16) & 0xFFFF, 16)
    w9 = (bits_of(twos(eph["af2"], 2 ** -55, 8), 8)
          + bits_of(twos(eph["af1"], 2 ** -43, 16), 16))
    w10 = bits_of(twos(eph["af0"], 2 ** -31, 22), 22) + [0, 0]
    return subframe([w3, w4, w5, w6, w7, w8, w9, w10], tow_count, 1)


# --- Subframes 2 & 3: Keplerian ephemeris (radians -> semicircles) ----

def _split(raw: int, n_hi: int, n_lo: int):
    return bits_of(raw >> n_lo, n_hi), bits_of(raw & ((1 << n_lo) - 1), n_lo)


def subframe2(eph: dict, tow_count: int) -> list[int]:
    iode = int(eph.get("iode", 0)) & 0xFF
    crs = twos(eph["crs"], 2 ** -5, 16)
    dn = twos(eph["delta_n"] / _math.pi, 2 ** -43, 16)
    m0_hi, m0_lo = _split(twos(eph["m0"] / _math.pi, 2 ** -31, 32), 8, 24)
    cuc = twos(eph["cuc"], 2 ** -29, 16)
    e_hi, e_lo = _split(twos(eph["e"], 2 ** -33, 32), 8, 24)
    cus = twos(eph["cus"], 2 ** -29, 16)
    a_hi, a_lo = _split(twos(eph["sqrtA"], 2 ** -19, 32), 8, 24)
    toe = round(eph["toe"] / 16) & 0xFFFF
    w3 = bits_of(iode, 8) + bits_of(crs, 16)
    w4 = bits_of(dn, 16) + m0_hi
    w5 = m0_lo
    w6 = bits_of(cuc, 16) + e_hi
    w7 = e_lo
    w8 = bits_of(cus, 16) + a_hi
    w9 = a_lo
    # AODO 31 = "not used", what almost every satellite broadcasts.
    w10 = bits_of(toe, 16) + [0] + bits_of(31, 5) + [0, 0]
    return subframe([w3, w4, w5, w6, w7, w8, w9, w10], tow_count, 2)


def subframe3(eph: dict, tow_count: int) -> list[int]:
    cic = twos(eph["cic"], 2 ** -29, 16)
    om0_hi, om0_lo = _split(twos(eph["omega0"] / _math.pi, 2 ** -31, 32), 8, 24)
    cis = twos(eph["cis"], 2 ** -29, 16)
    i0_hi, i0_lo = _split(twos(eph["i0"] / _math.pi, 2 ** -31, 32), 8, 24)
    crc = twos(eph["crc"], 2 ** -5, 16)
    w_hi, w_lo = _split(twos(eph["omega"] / _math.pi, 2 ** -31, 32), 8, 24)
    odot = twos(eph["omega_dot"] / _math.pi, 2 ** -43, 24)
    iode = int(eph.get("iode", 0)) & 0xFF
    idot = twos(eph["idot"] / _math.pi, 2 ** -43, 14)
    w3 = bits_of(cic, 16) + om0_hi
    w4 = om0_lo
    w5 = bits_of(cis, 16) + i0_hi
    w6 = i0_lo
    w7 = bits_of(crc, 16) + w_hi
    w8 = w_lo
    w9 = bits_of(odot, 24)
    w10 = bits_of(iode, 8) + bits_of(idot, 14) + [0, 0]
    return subframe([w3, w4, w5, w6, w7, w8, w9, w10], tow_count, 3)


# --- Subframes 4 & 5: 25-page support / almanac cycle ----------------
#
# SP-A produces these structurally: a receiver decoding a fix needs only
# SF1-3. SF4 page 18 carries the real Klobuchar iono + UTC parameters
# when the RINEX header supplies them; almanac pages carry a
# reduced-precision element set for the PRN they map to, or zeroed fields
# when no source exists.

_MU = 3.986005e14       # IS-GPS-200 Earth gravitational constant [m^3/s^2]
_WEEK_S = 604800.0


def _alm_toa_s(eph_by_prn: dict | None) -> float:
    """One almanac reference time for every page and SF5 page 25: the median
    ephemeris toe of the GPS records, on the 4096 s grid (IS-GPS-200 20.3.3.5.2.2)."""
    toes = sorted(float(r.get("toe", 0.0)) for r in
                  (_gps_alm(eph_by_prn, p) for p in range(1, 33)) if r)
    if not toes:
        return 0.0
    return (round(toes[len(toes) // 2] / 4096.0) * 4096.0) % _WEEK_S


def _alm_at_toa(a: dict, toa_s: float) -> tuple[float, float]:
    """Mean anomaly [rad] and clock bias af0 [s] propagated from the
    ephemeris epoch to the almanac reference time."""
    dt = (toa_s - float(a.get("toe", toa_s)) + _WEEK_S / 2) % _WEEK_S - _WEEK_S / 2
    sqrt_a = float(a.get("sqrtA", 0.0))
    n = _math.sqrt(_MU) / sqrt_a ** 3 + float(a.get("delta_n", 0.0)) if sqrt_a else 0.0
    m0 = float(a.get("m0", 0.0)) + n * dt
    m0 = (m0 + _math.pi) % (2 * _math.pi) - _math.pi
    dtc = (toa_s - float(a.get("toc", a.get("toe", toa_s))) + _WEEK_S / 2) % _WEEK_S - _WEEK_S / 2
    return m0, float(a.get("af0", 0.0)) + float(a.get("af1", 0.0)) * dtc


def _almanac_words(alm: dict | None, sv_id: int,
                   toa_s: float | None = None) -> list[list[int]]:
    """Almanac page words. ``toa_s`` is the common reference time; the record
    (an ephemeris) is propagated to it as a real almanac upload would be."""
    a = alm or {}

    def g(k, d=0.0):
        return a.get(k, d)

    if a and toa_s is None:
        toa_s = float(g("toa", g("toe")))
    m0_rad, af0_s = _alm_at_toa(a, toa_s) if a else (0.0, 0.0)
    e = twos(g("e"), 2 ** -21, 16)
    toa = int(round((toa_s or 0.0) / 4096)) & 0xFF
    di = twos(g("i0", 0.0) / _math.pi - 0.30, 2 ** -19, 16) if a else 0
    odot = twos(g("omega_dot") / _math.pi, 2 ** -38, 16) if a else 0
    # No record for the PRN: the SV is not in service (all-ones health), as a
    # real almanac marks an unusable satellite, not "healthy with no orbit".
    health = int(g("health", 0)) & 0xFF if a else 0xFF
    sqrta = int(round(g("sqrtA") / 2 ** -11)) & 0xFFFFFF
    om0 = twos(g("omega0") / _math.pi, 2 ** -23, 24) if a else 0
    w = twos(g("omega") / _math.pi, 2 ** -23, 24) if a else 0
    m0 = twos(m0_rad / _math.pi, 2 ** -23, 24) if a else 0
    af0 = twos(af0_s, 2 ** -20, 11)
    af1 = twos(g("af1"), 2 ** -38, 11)
    w3 = [0, 1] + bits_of(sv_id & 0x3F, 6) + bits_of(e, 16)
    w4 = bits_of(toa, 8) + bits_of(di, 16)
    w5 = bits_of(odot, 16) + bits_of(health, 8)
    w6 = bits_of(sqrta, 24)
    w7 = bits_of(om0, 24)
    w8 = bits_of(w, 24)
    w9 = bits_of(m0, 24)
    # word 10: af0 8 MSBs, af1 (11), af0 3 LSBs, 2 parity-solve bits
    w10 = bits_of(af0 >> 3, 8) + bits_of(af1, 11) + bits_of(af0 & 7, 3) + [0, 0]
    return [w3, w4, w5, w6, w7, w8, w9, w10]


def _iono_utc_words(header: dict | None, week: int = 0,
                    tow_count: int = 0) -> list[list[int]]:
    h = header or {}
    al = list(h.get("iono_alpha", [0, 0, 0, 0])) + [0, 0, 0, 0]
    be = list(h.get("iono_beta", [0, 0, 0, 0])) + [0, 0, 0, 0]
    u = gps_utc_params(h, week, tow_count * 6)
    w3 = ([0, 1] + bits_of(56, 6) + bits_of(twos(al[0], 2 ** -30, 8), 8)
          + bits_of(twos(al[1], 2 ** -27, 8), 8))
    w4 = (bits_of(twos(al[2], 2 ** -24, 8), 8) + bits_of(twos(al[3], 2 ** -24, 8), 8)
          + bits_of(twos(be[0], 2 ** 11, 8), 8))
    w5 = (bits_of(twos(be[1], 2 ** 14, 8), 8) + bits_of(twos(be[2], 2 ** 16, 8), 8)
          + bits_of(twos(be[3], 2 ** 16, 8), 8))
    a1 = twos(u.get("A1", 0.0), 2 ** -50, 24)
    a0 = twos(u.get("A0", 0.0), 2 ** -30, 32)
    w6 = bits_of(a1, 24)
    w7 = bits_of(a0 >> 8, 24)
    w8 = (bits_of(a0 & 0xFF, 8) + bits_of((int(u.get("tot", 0)) >> 12) & 0xFF, 8)
          + bits_of(int(u.get("WNt", 0)) & 0xFF, 8))
    w9 = (bits_of(twos(u.get("dtLS", 0), 1, 8), 8) + bits_of(int(u.get("WNlsf", 0)) & 0xFF, 8)
          + bits_of(int(u.get("DN", 0)) & 0xFF, 8))
    w10 = bits_of(twos(u.get("dtLSF", 0), 1, 8), 8) + bits_of(0, 14) + [0, 0]
    return [w3, w4, w5, w6, w7, w8, w9, w10]


# SF4 almanac pages 2-5, 7-10 -> PRN 25..32 (IS-GPS-200 Table 20-V).
_SF4_ALM = {2: 25, 3: 26, 4: 27, 5: 28, 7: 29, 8: 30, 9: 31, 10: 32}

# SVID carried by each SF4 page (IS-GPS-200 Table 20-V); the pages not built
# elsewhere (reserved, NMCT, special message) are sent as reserved pages.
_SF4_SVID = {1: 57, 6: 57, 11: 57, 16: 57, 21: 57, 12: 62, 13: 52, 14: 53,
             15: 54, 17: 55, 19: 58, 20: 59, 22: 60, 23: 61, 24: 62}


def _reserved_sf4_words(page: int) -> list[list[int]]:
    """Reserved SF4 page: data ID 01, the page's SVID, then the alternating
    1/0 fill a satellite sends in reserved bits (IS-GPS-200 20.3.3.5.1.1)."""
    fill = [1, 0] * 12
    w3 = [0, 1] + bits_of(_SF4_SVID.get(page, 57), 6) + fill[:16]
    return [w3] + [list(fill) for _ in range(7)]


def _gps_alm(eph_by_prn: dict | None, prn: int) -> dict | None:
    """GPS record for ``prn``. The simulator keys by ``(sys, prn)``; the bare
    ``prn`` key is shared with Galileo/BeiDou/GLONASS (same numbers), so it is
    used only when the map carries no system-qualified keys at all."""
    ebp = eph_by_prn or {}
    rec = ebp.get(("G", prn))
    if rec is None and not any(isinstance(k, tuple) for k in ebp):
        rec = ebp.get(prn)
    return rec


_SV_CONFIG = 0b1100     # A-S flag set + config code 100 (GPS III)


def _sf4_page25_words(eph_by_prn: dict | None) -> list[list[int]]:
    """SF4 page 25 (SVID 63): A-S flag + SV configuration of SV1..32 (4 bits
    each) and health of SV25..32 (6 bits each), IS-GPS-200 Fig. 20-1."""
    cfg = [bit for _ in range(32) for bit in bits_of(_SV_CONFIG, 4)]
    health = []
    for p in range(25, 33):
        rec = _gps_alm(eph_by_prn, p)
        health += bits_of(0 if rec is not None and not int(rec.get("health", 0)) else 0x3F, 6)
    w3 = [0, 1] + bits_of(63, 6) + cfg[0:16]
    w4, w5, w6, w7 = (cfg[16 + 24 * i:40 + 24 * i] for i in range(4))
    w8 = cfg[112:128] + [0, 0] + health[0:6]
    w9 = health[6:30]
    w10 = health[30:48] + bits_of(0, 6)
    return [w3, w4, w5, w6, w7, w8, w9, w10]


def subframe4(page: int, eph_by_prn: dict, header, tow_count: int,
              week: int = 0) -> list[int]:
    if page == 25:
        return subframe(_sf4_page25_words(eph_by_prn), tow_count, 4)
    if page == 18:
        return subframe(_iono_utc_words(header, week, tow_count), tow_count, 4)
    prn = _SF4_ALM.get(page)
    if prn is not None:
        return subframe(_almanac_words(_gps_alm(eph_by_prn, prn), prn,
                                       _alm_toa_s(eph_by_prn)), tow_count, 4)
    return subframe(_reserved_sf4_words(page), tow_count, 4)


def _sf5_page25_words(eph_by_prn: dict | None, week: int) -> list[list[int]]:
    """SF5 page 25 (SVID 51): almanac reference time, WNa, health of SV1..24."""
    toa = int(round(_alm_toa_s(eph_by_prn) / 4096)) & 0xFF
    health = []
    for p in range(1, 25):
        rec = _gps_alm(eph_by_prn, p)
        health += bits_of(0 if rec is not None and not int(rec.get("health", 0)) else 0x3F, 6)
    w3 = [0, 1] + bits_of(51, 6) + bits_of(toa, 8) + bits_of(int(week) & 0xFF, 8)
    words = [w3] + [health[i * 24:(i + 1) * 24] for i in range(6)]
    words.append(bits_of(0, 24))
    return words


def subframe5(page: int, eph_by_prn: dict, tow_count: int, week: int = 0) -> list[int]:
    if page == 25:
        return subframe(_sf5_page25_words(eph_by_prn, week), tow_count, 5)
    prn = page if 1 <= page <= 24 else None
    alm = _gps_alm(eph_by_prn, prn) if prn else None
    return subframe(_almanac_words(alm, prn or 0, _alm_toa_s(eph_by_prn)),
                    tow_count, 5)


# --- Frame + run-length stream --------------------------------------

NAV_BIT_HZ = 50


def frame_bits(eph: dict, header, week: int, tow_count: int,
               sf45_page: int, eph_by_prn: dict | None = None,
               sf4_page: int | None = None) -> list[int]:
    """1500 bits: SF1, SF2, SF3, SF4[page], SF5[page]. Each subframe's
    TOW-count increments by one. ``sf4_page`` overrides the SF4 page only."""
    ebp = eph_by_prn or {}
    _TLM_MSG[0] = week & 0x3FF
    if sf4_page is None:
        sf4_page = sf45_page
    return (subframe1(eph, week, tow_count)
            + subframe2(eph, tow_count + 1)
            + subframe3(eph, tow_count + 2)
            + subframe4(sf4_page, ebp, header, tow_count + 3, week)
            + subframe5(sf45_page, ebp, tow_count + 4, week))


def nav_stream(eph: dict, header, week: int, tow0_sow: float,
               duration_s: float, eph_by_prn: dict | None = None):
    """Run-length LNAV symbol stream as ``numpy.int8`` in {+1, -1}.

    The first subframe boundary sits on the 6-second TOW grid at or
    before ``tow0_sow``; symbols run for ``duration_s`` plus a 30-second
    guard. Data bit 0 -> +1, bit 1 -> -1. Deterministic for a fixed
    (eph, header, week, tow0, duration).
    """
    start = int(tow0_sow // 6) * 6
    tc0 = start // 6
    # Subframe id and SF4/5 page follow GPS time, not the stream start: a
    # frame (SF1..SF5) begins every 30 s of the week and the 25-page cycle
    # restarts at the week start (IS-GPS-200 20.3.4.1). Starting every
    # stream at SF1 made each 1 s live segment re-send SF1 only, so a
    # receiver never saw SF2/SF3 and could not collect the ephemeris.
    f0 = tc0 // 5
    skip = (tc0 - f0 * 5) * 300
    need = int(NAV_BIT_HZ * (_math.ceil(duration_s) + 30))
    n_frames = (skip + need) // 1500 + 2
    bits: list[int] = []
    for k in range(n_frames):
        frame = f0 + k
        bits += frame_bits(eph, header, week, frame * 5,
                           (frame % 25) + 1, eph_by_prn)
    bits = bits[skip:skip + need]
    arr = _np.array([1 if b == 0 else -1 for b in bits], dtype=_np.int8)
    return arr
