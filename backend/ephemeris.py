# backend/ephemeris.py
from __future__ import annotations

import datetime as dt
import gzip
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
    "health": "health",
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


def get_ephemeris(date: dt.date, download: bool = True) -> dict[int, dict]:
    p = _cache_path(date)
    if not p.exists():
        if not download:
            raise EphemerisUnavailable(f"no cached RINEX for {date}")
        p = _download(date)
        if p is None:
            raise EphemerisUnavailable(f"all mirrors failed for {date}")
    return parse_rinex(p)


def cached_rinex_path(date: dt.date) -> pathlib.Path:
    """Ensure a RINEX file for ``date`` is cached, then return its local path."""
    get_ephemeris(date)
    return _cache_path(date)
