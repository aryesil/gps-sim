# backend/nmea.py
"""Parse NMEA 0183 sentences from a real GNSS receiver (the closed-loop
check: does hardware that actually received our simulated RF report the
position/time we intended?). Pure functions, no I/O -- backend/receiver_feed.py
owns the serial/UDP listener that hands sentences here.
"""
from __future__ import annotations


class NmeaError(Exception):
    pass


def _checksum_ok(sentence: str) -> bool:
    if "*" not in sentence:
        return False
    body, _, cksum = sentence.rpartition("*")
    body = body.lstrip("$")
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    try:
        return calc == int(cksum.strip(), 16)
    except ValueError:
        return False


def _dm_to_deg(value: str, hemi: str) -> float:
    """NMEA lat/lon are ddmm.mmmm / dddmm.mmmm -- degrees + decimal minutes,
    hemisphere-signed."""
    if not value:
        return 0.0
    dot = value.index(".")
    deg_len = dot - 2
    deg = float(value[:deg_len])
    minutes = float(value[deg_len:])
    d = deg + minutes / 60.0
    return -d if hemi in ("S", "W") else d


def parse_gga(sentence: str) -> dict:
    """$GPGGA/$GNGGA: fix quality, position, satellite count, HDOP, altitude."""
    if not _checksum_ok(sentence):
        raise NmeaError(f"bad checksum: {sentence!r}")
    body = sentence.split("*")[0]
    f = body.split(",")
    if len(f) < 10 or not f[0].endswith("GGA"):
        raise NmeaError(f"not a GGA sentence: {sentence!r}")
    fix_quality = int(f[6]) if f[6] else 0
    return {
        "sentence": "GGA",
        "utc_time": f[1],
        "lat": _dm_to_deg(f[2], f[3]),
        "lon": _dm_to_deg(f[4], f[5]),
        "fix_quality": fix_quality,
        "num_sats": int(f[7]) if f[7] else 0,
        "hdop": float(f[8]) if f[8] else None,
        "alt_m": float(f[9]) if f[9] else None,
    }


def parse_rmc(sentence: str) -> dict:
    """$GPRMC/$GNRMC: position, ground speed/course, active/void status."""
    if not _checksum_ok(sentence):
        raise NmeaError(f"bad checksum: {sentence!r}")
    body = sentence.split("*")[0]
    f = body.split(",")
    if len(f) < 10 or not f[0].endswith("RMC"):
        raise NmeaError(f"not an RMC sentence: {sentence!r}")
    return {
        "sentence": "RMC",
        "utc_time": f[1],
        "status": "active" if f[2] == "A" else "void",
        "lat": _dm_to_deg(f[3], f[4]),
        "lon": _dm_to_deg(f[5], f[6]),
        "speed_knots": float(f[7]) if f[7] else 0.0,
        "course_deg": float(f[8]) if f[8] else 0.0,
        "date": f[9],
    }


_PARSERS = {"GGA": parse_gga, "RMC": parse_rmc}


def parse(sentence: str) -> dict | None:
    """Dispatch on sentence type (last 3 chars of the talker+type field,
    e.g. GPGGA/GNGGA -> GGA). Returns None for a recognized-but-unparsed
    or malformed sentence rather than raising, so a listener loop can
    just skip whatever a receiver sends that we don't handle."""
    sentence = sentence.strip()
    if not sentence.startswith("$") or len(sentence) < 6:
        return None
    kind = sentence[3:6]
    parser = _PARSERS.get(kind)
    if parser is None:
        return None
    try:
        return parser(sentence)
    except NmeaError:
        return None
