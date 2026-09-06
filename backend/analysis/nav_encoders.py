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
}


def nav_stream_for(sysc, signal, eph, header, week, sow, duration_s,
                   eph_by_prn=None, prn=None):
    """Dispatch to the per-system encoder. ``eph`` is that satellite's parsed
    broadcast record (dict); ``signal`` is its ``signals.Signal``; ``prn`` is
    the constellation PRN (used to pick BeiDou D1 vs D2).
    """
    if sysc in ("G", "J"):
        # QZSS L1 C/A LNAV is frame-identical to GPS LNAV -- same subframes,
        # TLM/HOW, parity. The PRN (193..202 for QZSS) does not enter the bit
        # content, so the GPS encoder is reused verbatim.
        arr = lnav_encode.nav_stream(eph, header or {}, week, sow, duration_s,
                                     eph_by_prn=eph_by_prn)
        return arr, 50.0
    if sysc == "E":
        arr = inav_encode.nav_stream(eph, week, sow, duration_s,
                                     eph_by_prn=eph_by_prn)
        return arr, inav_encode.SYM_RATE_HZ
    if sysc == "C":
        p = prn if prn is not None else int(eph.get("prn", 99) or 99)
        arr, rate = bds_d1_encode.nav_stream(eph, week, sow, duration_s,
                                             d2=(p <= 5),
                                             eph_by_prn=eph_by_prn)
        return arr, rate
    if sysc == "R":
        return glo_str_encode.nav_stream(eph, week, sow, duration_s,
                                         eph_by_prn=eph_by_prn)
    if sysc == "S":
        return sbas_encode.nav_stream(eph, week, sow, duration_s,
                                      eph_by_prn=eph_by_prn)
    return None
