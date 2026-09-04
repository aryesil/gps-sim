# backend/ephemeris.py
from __future__ import annotations

import datetime as dt
import gzip
import math
import pathlib

import georinex as gr
import numpy as np
import requests

from backend import config


class EphemerisUnavailable(Exception):
    pass


_VARMAP = {
    "toe": "Toe", "toc": None, "sqrtA": "sqrtA", "e": "Eccentricity",
    "m0": "M0", "delta_n": "DeltaN", "omega": "omega", "omega0": "Omega0",
    "omega_dot": "OmegaDot", "i0": "Io", "idot": "IDOT",
    "cuc": "Cuc", "cus": "Cus", "crc": "Crc", "crs": "Crs",
    "cic": "Cic", "cis": "Cis", "af0": "SVclockBias", "af1": "SVclockDrift",
    "af2": "SVclockDriftRate", "tgd": "TGD", "gps_week": "GPSWeek",
    "health": "health", "iode": "IODE", "iodc": "IODC", "codes_l2": "CodesL2",
}


_GPS_EPOCH = dt.datetime(1980, 1, 6)
_WEEK_SECONDS = 604800.0


def _to_datetime(ts) -> dt.datetime:
    """numpy datetime64 -> aware-naive UTC datetime."""
    secs = np.datetime64(ts, "s").astype("int64")
    return dt.datetime(1970, 1, 1) + dt.timedelta(seconds=int(secs))


def _gps_seconds(when: dt.datetime) -> float:
    """Continuous seconds since the GPS epoch (leap seconds ignored)."""
    return (when - _GPS_EPOCH).total_seconds()


def _seconds_of_week(when: dt.datetime) -> float:
    return _gps_seconds(when) % _WEEK_SECONDS


def gps_week_and_sow(when_gps: dt.datetime) -> tuple[int, float]:
    """GPS week number and seconds-of-week for a GPS-timescale datetime
    (leap seconds already applied by the caller, as scenario.build_args does
    for gps-sdr-sim's -t/-T)."""
    total = _gps_seconds(when_gps)
    return int(total // _WEEK_SECONDS), total % _WEEK_SECONDS


def align_epochs(eph_by_prn: dict[int, dict], week: int, sow: float) -> dict[int, dict]:
    """Return a copy of eph_by_prn with every satellite's toc/toe/gps_week
    overwritten to the same (week, sow).

    A downloaded daily BRDC file has each satellite's own broadcast epoch --
    parse_rinex already picked, per PRN, whichever one is nearest the file's
    midday, so those epochs can be many hours apart from each other. Feeding
    that straight to gps-sdr-sim with -T only realigns the file to the first
    satellite it finds (see KNOWN_ISSUES); every other satellite keeps its
    original, possibly far-off epoch, and gps-sdr-sim aborts with
    "No current set of ephemerides has been found" as soon as none of them
    land within its ±1h window. Aligning every satellite to the requested
    start ourselves keeps the approximation (same one -t/-T always makes:
    tk=0, no orbit propagation) but bounds it uniformly instead of leaving it
    to chance which satellite gps-sdr-sim's shift happens to land on.
    """
    out = {}
    for prn, e in eph_by_prn.items():
        e2 = dict(e)
        e2["toc"] = sow
        e2["toe"] = sow
        e2["gps_week"] = week
        out[prn] = e2
    return out


def _canonical_name(date: dt.date) -> str:
    return f"BRDC_{date:%Y%j}.rnx"


def _cache_path(date: dt.date) -> pathlib.Path:
    return config.DATA_DIR / "rinex" / _canonical_name(date)


def save_uploaded_rinex(date: dt.date, raw: bytes) -> pathlib.Path:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    p = _cache_path(date)
    p.write_bytes(raw)
    return p


def _download(date: dt.date) -> pathlib.Path | None:
    ddd = f"{date.timetuple().tm_yday:03d}"
    for tmpl in config.RINEX_MIRRORS:
        url = tmpl.format(yyyy=date.year, ddd=ddd)
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
        except requests.RequestException:
            continue
        content = r.content
        data = gzip.decompress(content) if content[:2] == b"\x1f\x8b" else content
        if b"RINEX VERSION" not in data[:200]:
            continue  # mirror returned an error page / HTML, not a RINEX file
        p = _cache_path(date)
        p.write_bytes(data)
        return p
    return None


def parse_rinex(path: str | pathlib.Path) -> dict[int, dict]:
    nav = gr.load(str(path), use="G")
    # File's calendar day -> that day's noon UTC, as continuous GPS seconds.
    mid_day = _to_datetime(nav.time.values[len(nav.time) // 2])
    noon = dt.datetime(mid_day.year, mid_day.month, mid_day.day, 12, 0, 0)
    noon_gps = _gps_seconds(noon)
    out: dict[int, dict] = {}
    for sv in nav.sv.values:
        if not str(sv).startswith("G"):
            continue
        prn = int(str(sv)[1:])
        sub = nav.sel(sv=sv).dropna(dim="time", how="all")
        if sub.time.size == 0:
            continue
        # Pick the epoch whose toe is nearest the file's midday. Compare on
        # absolute GPS time (week*604800 + toe) so a week rollover in the file
        # cannot select the wrong set.
        best_i, best_d = 0, None
        for i in range(int(sub.time.size)):
            r = sub.isel(time=i)
            toe_gps = float(r["GPSWeek"].values) * _WEEK_SECONDS + float(r["Toe"].values)
            d = abs(toe_gps - noon_gps)
            if best_d is None or d < best_d:
                best_i, best_d = i, d
        rec = sub.isel(time=best_i)
        e: dict[str, float] = {}
        for key, var in _VARMAP.items():
            if var is None:
                continue
            e[key] = float(rec[var].values)
        # toc is the record's own clock reference epoch (distinct field from toe).
        e["toc"] = _seconds_of_week(_to_datetime(rec.time.values))
        out[prn] = e
    if not out:
        raise EphemerisUnavailable("no GPS ephemeris in file")
    return out


def _rinex2_field(v: float) -> str:
    """A single 19-char scientific-notation field, gps-sdr-sim's fixed-width
    RINEX-2 nav parser reads at 19-char offsets and passes straight to atof()
    (any 'D'/'E' exponent both work)."""
    if v is None or not math.isfinite(v):
        v = 0.0
    return f"{float(v):19.12E}"


def to_rinex2_nav(eph_by_prn: dict[int, dict]) -> str:
    """Serialize a parsed ephemeris dict (as returned by parse_rinex /
    get_ephemeris) into a RINEX-2 GPS nav file.

    gps-sdr-sim's bundled `readRinexNavAll()` only understands RINEX-2 nav
    (see KNOWN_ISSUES F2) even though `georinex`/this module parse RINEX-3
    fine. generator.run uses this to re-serialize whatever RINEX version was
    resolved into a file gps-sdr-sim can always read, instead of patching or
    forking gps-sdr-sim's C parser.
    """
    f = _rinex2_field
    lines = [
        "     2              NAVIGATION DATA                        RINEX VERSION / TYPE".ljust(73),
        "".ljust(60) + "END OF HEADER".ljust(20),
    ]
    for prn in sorted(eph_by_prn):
        e = eph_by_prn[prn]
        week = int(e["gps_week"])
        when = _GPS_EPOCH + dt.timedelta(weeks=week, seconds=e["toc"])
        epoch = (f"{prn:2d} {when.year % 100:02d} {when.month:02d} {when.day:02d} "
                 f"{when.hour:02d} {when.minute:02d} {when.second:4.1f}")
        lines.append(epoch + f(e["af0"]) + f(e["af1"]) + f(e["af2"]))
        lines.append("   " + f(e.get("iode", 0.0)) + f(e["crs"]) + f(e["delta_n"]) + f(e["m0"]))
        lines.append("   " + f(e["cuc"]) + f(e["e"]) + f(e["cus"]) + f(e["sqrtA"]))
        lines.append("   " + f(e["toe"]) + f(e["cic"]) + f(e["omega0"]) + f(e["cis"]))
        lines.append("   " + f(e["i0"]) + f(e["crc"]) + f(e["omega"]) + f(e["omega_dot"]))
        lines.append("   " + f(e["idot"]) + f(e.get("codes_l2", 0.0)) + f(week) + f(0.0))
        lines.append("   " + f(0.0) + f(e.get("health", 0.0)) + f(e["tgd"]) + f(e.get("iodc", 0.0)))
        lines.append("   " + f(0.0) + f(0.0) + f(0.0) + f(0.0))
    return "\n".join(lines) + "\n"


def get_ephemeris(date: dt.date, download: bool = True) -> dict[int, dict]:
    p = _cache_path(date)
    if not p.exists():
        if not download:
            raise EphemerisUnavailable(f"no cached RINEX for {date}")
        p = _download(date)
        if p is None:
            raise EphemerisUnavailable(f"all mirrors failed for {date}")
    try:
        return parse_rinex(p)
    except EphemerisUnavailable:
        raise
    except Exception as e:
        raise EphemerisUnavailable(f"could not parse cached RINEX {p}: {e}") from e


def cached_rinex_path(date: dt.date) -> pathlib.Path:
    """Ensure a RINEX file for ``date`` is cached, then return its local path."""
    get_ephemeris(date)
    return _cache_path(date)
