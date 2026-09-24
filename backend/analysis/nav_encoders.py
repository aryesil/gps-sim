"""SP-D: per-constellation broadcast navigation message dispatch.

``nav_stream_for`` returns ``(symbols, sym_rate_hz)`` where ``symbols`` is an
``int8`` array in ``{-1, +1}`` sampled at ``sym_rate_hz``, ready to hand to
the native mixer's ``NavSource`` (``SvSpec.nav_bits`` / ``nav_nbits`` /
``nav_sym_rate_hz``). Returns ``None`` for a system with no encoder yet, in
which case the engine leaves that SV's data symbol constant (pre-SP-D
behaviour).

The GPS/QZSS L1 C/A message is IS-GPS-200 / IS-QZSS-PNT LNAV and is built by
:mod:`backend.analysis.lnav_encode`. Galileo I/NAV, BeiDou D1/D2, GLONASS
strings and SBAS land in follow-up tiers; their hooks are wired here so the
engine side is complete.
"""
from __future__ import annotations

from backend.analysis import (bds_d1_encode, glo_str_encode, inav_encode,
                              lnav_encode, sbas_encode)

# Nominal symbol rates (Hz) per system's supported message.
SYM_RATE_HZ = {
    "G": 50.0,     # LNAV
    "J": 50.0,     # QZSS L1 C/A LNAV (frame-compatible with GPS)
    "E": 250.0,    # E1-B I/NAV (after r=1/2 FEC)
    "C": 50.0,     # B1I D1 (D2 GEO is 500, handled in that tier)
    "R": 100.0,    # L1OF, 50 bps with 100 Hz meander
    "S": 500.0,    # SBAS L1, 250 bps after r=1/2 FEC
    "I": 50.0,     # NavIC L5-SPS subframes 1/2
}


def nav_stream_for(sysc, signal, eph, header, week, sow, duration_s,
                   eph_by_prn=None, prn=None):
    """Dispatch to the per-system encoder. ``eph`` is that satellite's parsed
    broadcast record (dict); ``signal`` is its ``signals.Signal``; ``prn`` is
    the constellation PRN (used to pick BeiDou D1 vs D2).
    """
    band = getattr(signal, "band", "L1")
    if band in ("L2", "L5") and sysc in ("G", "J"):
        # GPS / QZSS L2C and L5 carry CNAV (IS-GPS-200 Sec. 30 / 40), not
        # LNAV. Same message set on both bands; QZSS reuses it verbatim.
        from backend.analysis import cnav_encode
        p = int(prn if prn is not None else (eph.get("prn", 1) or 1))
        arr, rate = cnav_encode.nav_stream(eph, header or {}, week, sow,
                                           duration_s, prn=p,
                                           eph_by_prn=eph_by_prn)
        return arr, rate
    if sysc in ("G", "J"):
        # QZSS L1 C/A LNAV is frame-identical to GPS LNAV -- same subframes,
        # TLM/HOW, parity. The PRN (193..202 for QZSS) does not enter the bit
        # content, so the GPS encoder is reused verbatim.
        arr = lnav_encode.nav_stream(eph, header or {}, week, sow, duration_s,
                                     eph_by_prn=eph_by_prn)
        return arr, 50.0
    if band == "L5" and sysc == "E":
        # Galileo E5a-I carries F/NAV (Galileo OS SIS ICD Sec. 4.2), not
        # I/NAV -- word types 1-4 only (see fnav_encode's module docstring).
        from backend.analysis import fnav_encode
        p = int(prn if prn is not None else (eph.get("prn", 1) or 1))
        arr, rate = fnav_encode.nav_stream(eph, p, week, sow, duration_s,
                                           eph_by_prn=eph_by_prn)
        return arr, rate
    if band == "L5" and sysc == "C":
        # BeiDou B2a-data carries B-CNAV2 (BDS-SIS-ICD-B2a-1.0 Sec. 6), not
        # D1/D2 -- message types 10/11/30 only (see bcnav2_encode's module
        # docstring).
        from backend.analysis import bcnav2_encode
        p = int(prn if prn is not None else (eph.get("prn", 1) or 1))
        e_b, wk_b, sow_b = to_bdt(eph, week, sow)
        arr, rate = bcnav2_encode.nav_stream(e_b, p, wk_b, sow_b, duration_s,
                                             eph_by_prn=eph_by_prn)
        return arr, rate
    if sysc == "I":
        # NavIC (IRNSS) L5-SPS subframes 1/2 (ISRO-IRNSS-ICD-SPS-1.1) --
        # its own framing/FEC/interleave, not CNAV or LNAV.
        from backend.analysis import navic_encode
        p = int(prn if prn is not None else (eph.get("prn", 1) or 1))
        arr, rate = navic_encode.nav_stream(eph, header or {}, week, sow,
                                            duration_s, prn=p,
                                            eph_by_prn=eph_by_prn)
        return arr, rate
    if sysc == "E":
        arr = inav_encode.nav_stream(eph, week, sow, duration_s,
                                     eph_by_prn=eph_by_prn)
        return arr, inav_encode.SYM_RATE_HZ
    if sysc == "C":
        p = prn if prn is not None else int(eph.get("prn", 99) or 99)
        # GEO satellites (C01-C05 and C59-C63) broadcast D2, not D1.
        e_b, wk_b, sow_b = to_bdt(eph, week, sow)
        # Broadcast the Klobuchar set the engine applied (same scaling as
        # GPS), so a receiver removes the delay the signal carries.
        h = header or {}
        iono = (h.get("iono_alpha") or (0.0,) * 4,
                h.get("iono_beta") or (0.0,) * 4)
        arr, rate = bds_d1_encode.nav_stream(e_b, wk_b, sow_b, duration_s,
                                             d2=(p <= 5 or p >= 59),
                                             eph_by_prn=eph_by_prn, iono=iono)
        return arr, rate
    if sysc == "R":
        return glo_str_encode.nav_stream(eph, week, sow, duration_s,
                                         eph_by_prn=eph_by_prn)
    if sysc == "S":
        return sbas_encode.nav_stream(eph, week, sow, duration_s,
                                      eph_by_prn=eph_by_prn)
    return None


# BeiDou Time runs 14 s behind GPS time and its week count starts 1356 GPS
# weeks later (BDS-SIS-ICD 5.1.2).
BDT_MINUS_GPS_S = -14.0
BDT_WEEK_OFFSET = 1356


def to_bdt(eph: dict, week: int, sow: float):
    """GPS-time (eph, week, sow) -> BDT for the BeiDou encoders. The engine
    aligns every Keplerian toe/toc on the GPS scale; a BeiDou receiver
    evaluates them on BDT, so they are shifted by the same -14 s as the
    broadcast SOW (the orbit itself is unchanged: tk = t_bdt - toe_bdt)."""
    e = dict(eph)
    for k in ("toe", "toc"):
        if k in e and e[k] is not None:
            e[k] = (float(e[k]) + BDT_MINUS_GPS_S) % 604800.0
    return e, int(week) - BDT_WEEK_OFFSET, float(sow) + BDT_MINUS_GPS_S


def stream_t0_sow(sysc, signal, tow0_sow: float, prn=None) -> float:
    """GPS seconds-of-week at which symbol 0 of ``nav_stream_for(...,
    tow0_sow, ...)`` leaves the satellite. Mirrors each encoder's own grid
    rule; the engine uses it to lock the mixer's transmit-time clock to the
    stream (``SvSpec.tx_chips_offset``)."""
    import math
    band = getattr(signal, "band", "L1")
    t = float(tow0_sow)
    if band in ("L2", "L5") and sysc in ("G", "J"):
        return (t // 12.0) * 12.0                        # CNAV, 12 s messages
    if sysc in ("G", "J"):
        return (t // 6.0) * 6.0                          # LNAV subframes
    if band == "L5" and sysc == "E":
        r = int(round(t))
        return float(r - r % 10)                         # F/NAV 10 s pages
    if band == "L5" and sysc == "C":
        b = int(round(t + BDT_MINUS_GPS_S))
        return float(b - b % 3) - BDT_MINUS_GPS_S        # B-CNAV2, BDT grid
    if sysc == "I":
        return (t // 12.0) * 12.0                        # NavIC subframes
    if sysc == "E":
        r = int(round(t))
        return float(r - r % 2)                          # I/NAV 2 s pages
    if sysc == "C":
        p = int(prn if prn is not None else 99)
        grid = 3 if (p <= 5 or p >= 59) else 6
        b = t + BDT_MINUS_GPS_S
        return math.floor(b / grid) * grid - BDT_MINUS_GPS_S
    if sysc == "R":
        return (t // 2.0) * 2.0                          # 2 s strings
    if sysc == "S":
        return float(math.floor(t))                      # 1 s blocks
    return t
