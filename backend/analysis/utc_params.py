"""GPS-UTC parameters for the LNAV (SF4 page 18) and CNAV (MT33) broadcast.

A real satellite always sends a complete, self-consistent UTC block. The RINEX
headers the simulator uses (BKG BRDC) carry no GPUT line, only
``LEAP SECONDS``, so every field used to default to zero: GPS then claimed
GPS-UTC = 0 s, WNt = 0 and an invalid DN = 0 while the receiver (and every
other constellation in the same scenario) knew the offset is 18 s.
"""
from __future__ import annotations

from backend import config

# Most recent leap second as the satellites still broadcast it (IS-GPS-200
# 20.3.3.5.2.4): 2016-12-31 end of day, GPS week 1929, day number 7
# (1 = Sunday .. 7 = Saturday). dtLS = dtLSF = 18 s and the event is in the past.
_LAST_EVENT_WEEK = 1929
_LAST_EVENT_DN = 7
_LAST_EVENT_LEAP_S = 18


def gps_utc_params(header: dict | None, week: int, sow: float) -> dict:
    """Complete GPS UTC block: keys ``A0 A1 tot WNt dtLS WNlsf DN dtLSF``.

    Values given in ``header['utc']`` (RINEX GPUT) win; anything missing is
    filled from the header leap-second count (``header['leap_s']``, else
    ``config.GPS_UTC_LEAP_S``). ``tot`` is the reference time in seconds
    (multiple of 4096), ``WNt`` / ``WNlsf`` are 8-bit week numbers and
    ``WNlsf_full`` is the unwrapped week of the last leap-second event.
    """
    h = header or {}
    u = dict(h.get("utc") or {})
    leap = int(round(float(h.get("leap_s", config.GPS_UTC_LEAP_S))))
    wn8 = int(week) & 0xFF
    if leap == _LAST_EVENT_LEAP_S:
        wn_lsf_full, dn = _LAST_EVENT_WEEK, _LAST_EVENT_DN
    else:
        wn_lsf_full, dn = int(week), _LAST_EVENT_DN
    return {
        "A0": float(u.get("A0", 0.0)),
        "A1": float(u.get("A1", 0.0)),
        "tot": int(u.get("tot", (int(sow) // 4096) * 4096)),
        "WNt": int(u.get("WNt", wn8)) & 0xFF,
        "dtLS": int(u.get("dtLS", leap)),
        "WNlsf": int(u.get("WNlsf", wn_lsf_full)) & 0xFF,
        "WNlsf_full": wn_lsf_full,                   # CNAV MT33 sends 13 bits
        "DN": int(u.get("DN", dn)),
        "dtLSF": int(u.get("dtLSF", u.get("dtLS", leap))),
    }
