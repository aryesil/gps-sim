# backend/ephemeris.py
from __future__ import annotations

import datetime as dt
import gzip
import io
import pathlib

import georinex as gr
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
        data = gzip.decompress(r.content) if url.endswith(".gz") else r.content
        p = _cache_path(date)
        p.write_bytes(data)
        return p
    return None


def parse_rinex(path: str | pathlib.Path) -> dict[int, dict]:
    nav = gr.load(str(path), use="G")
    mid = nav.time.values[len(nav.time) // 2]
    out: dict[int, dict] = {}
    for sv in nav.sv.values:
        if not str(sv).startswith("G"):
            continue
        prn = int(str(sv)[1:])
        sub = nav.sel(sv=sv).dropna(dim="time", how="all")
        if sub.time.size == 0:
            continue
        idx = int(abs(sub.time.values - mid).argmin())
        rec = sub.isel(time=idx)
        e: dict[str, float] = {}
        for key, var in _VARMAP.items():
            if var is None:
                continue
            e[key] = float(rec[var].values)
        e.setdefault("toc", e["toe"])
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
