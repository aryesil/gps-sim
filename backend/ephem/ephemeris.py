# backend/ephemeris.py
from __future__ import annotations

import datetime as dt
import gzip
import logging
import math
import pathlib
import time

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


_VARMAP_KEPLER = dict(_VARMAP)   # GPS map; QZSS + Galileo + BeiDou share it
# BeiDou records name their clock/health fields differently (RINEX 3 BDS:
# TGD1/TGD2, AODE/AODC, SatH1); without this TGD1 was silently dropped.
_VARMAP_BDS = dict(_VARMAP_KEPLER, tgd="TGD1", tgd2="TGD2", iode="AODE",
                   iodc="AODC", health="SatH1")
# Galileo records (RINEX 3 GAL): IODnav, BGD(E1,E5b) for I/NAV E1 users,
# BGD(E1,E5a) for F/NAV E5a users, SISA in metres, GALWeek. Without this
# map IODnav/BGD/SISA were silently absent and every word went out as 0.
_VARMAP_GAL = dict(_VARMAP_KEPLER, iode="IODnav", tgd="BGDe5b",
                   tgd_e5a="BGDe5a", sisa="SISA", gps_week="GALWeek")
_VARMAP_GAL.pop("iodc")

# RINEX week field per Keplerian system, and its offset to the GPS week.
# GALWeek is already aligned to the GPS week (RINEX 3.04 Table A8); BDTWeek
# counts from 2006-01-01 and BDT runs 14 s behind GPS time.
_WEEK_VAR = {"E": ("GALWeek", 0, 0.0), "C": ("BDTWeek", 1356, 14.0)}

# ECEF-state systems (GLONASS "R", SBAS "S"): position/velocity/accel vector
# plus clock terms, expressed directly in km / km s^-1 / km s^-2 by RINEX 3.
# "glo_k" (FreqNum) is the GLONASS FDMA channel number -- carried from the
# start for a later task even though the state integrator does not need it.
_VARMAP_STATE = {
    "x_m": "X", "y_m": "Y", "z_m": "Z",
    "vx": "dX", "vy": "dY", "vz": "dZ",
    "ax": "dX2", "ay": "dY2", "az": "dZ2",
    "tau": "SVclockBias", "gamma": "SVrelFreqBias",
    "frame_time": "MessageFrameTime",
    "glo_k": "FreqNum",
}

_KEPLER_SYS = frozenset("GJECI")
_STATE_SYS = frozenset("RS")


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


# Keplerian toe/toc grid the native engine aligns to (align_epochs
# kepler_grid_s). Anything rebuilding a native run's "expected" geometry must
# use the same grid, or it compares against a different orbit.
NATIVE_TOE_GRID_S = 3600.0


def toe_grid_for(engine: str | None) -> float:
    return NATIVE_TOE_GRID_S if engine == "native" else 0.0


# Systems whose RINEX toe/week are on the GPS time scale (GPSWeek field),
# so a record can be used with its REAL broadcast epoch.
_REAL_EPOCH_SYS = ("G", "J")
# A real record stays in use up to 4 h from its toe: past the nominal
# +/-2 h fit it drifts only tens of metres, while relabelling puts the
# satellite on a fictitious orbit (a receiver with real assistance data then
# sees a satellite that is not in the sky). The daily BRDC also lags the
# uploads, so at hh:01 the newest record of some satellites is 2 h old.
REAL_EPH_WINDOW_S = 14400.0


_GLO_TB_GRID_S = 900.0        # GLONASS t_b: 15 min steps of Moscow time
_GLO_REAL_WINDOW_S = 900.0    # a real GLONASS state is good for ~+/-15 min


def _glonass_toe_ref(utc_sow: float, sow: float, grid_s: float,
                     keep_real_within_s: float) -> float:
    """GPS-SoW reference epoch for a GLONASS state record.

    The parser gives the record's own epoch on the UTC scale (RINEX
    GLONASS epochs are UTC). With ``keep_real_within_s`` set and that epoch
    within 15 min of the run, the real state is kept at its real epoch,
    converted to GPS time. Otherwise the state is relabelled to the run
    start -- snapped down onto the 15 min Moscow-time grid when ``grid_s``
    is set (native engine), because the broadcast t_b field can only carry
    that grid and a receiver propagates from t_b."""
    real = float(utc_sow) + config.GPS_UTC_LEAP_S
    d = (real - float(sow) + _WEEK_SECONDS / 2) % _WEEK_SECONDS - _WEEK_SECONDS / 2
    if keep_real_within_s > 0 and abs(d) <= _GLO_REAL_WINDOW_S:
        return float(sow) + d
    if grid_s and grid_s > 0:
        u = float(sow) - config.GPS_UTC_LEAP_S
        return (u // _GLO_TB_GRID_S) * _GLO_TB_GRID_S + config.GPS_UTC_LEAP_S
    return float(sow)


def align_epochs(eph_by_prn: dict[int, dict], week: int, sow: float,
                 kepler_grid_s: float = 0.0,
                 keep_real_within_s: float = 0.0) -> dict[int, dict]:
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

    ``keep_real_within_s`` > 0: a GPS/QZSS record whose own broadcast epoch
    is within that many seconds of (week, sow) is kept UNCHANGED -- the
    real ephemeris, i.e. the real sky. Relabeling moves every satellite to
    a fictitious orbit position; a receiver that also knows the real
    ephemeris (any phone: A-GPS/SUPL assistance + network time) then only
    searches the satellites that are really up, with the real Doppler, and
    rejects ours. Only records too far from the run start are relabeled.

    ``kepler_grid_s`` > 0 snaps the Keplerian toe/toc down to that grid
    instead of using ``sow`` itself. The native engine needs this: the
    broadcast toe/toc fields are quantised (LNAV 16 s, CNAV/B-CNAV2 300 s,
    I/NAV 60 s, D1 8 s), so an off-grid toe is transmitted rounded and a
    real receiver then propagates a different orbit than the one the IQ was
    generated from (tens of km at an 8 s toe error). A whole-hour grid is a
    multiple of every one of those resolutions.
    """
    k_sow = k_sow_bds = sow
    if kepler_grid_s and kepler_grid_s > 0:
        g = float(kepler_grid_s)
        k_sow = (float(sow) // g) * g
        # BeiDou broadcasts toe/toc on BDT = GPS - 14 s: snap on that scale.
        k_sow_bds = ((float(sow) - 14.0) // g) * g + 14.0
    out = {}
    for prn, e in eph_by_prn.items():
        e2 = dict(e)
        if "toe" not in e2 and "toe_ref" in e2:
            # ECEF-state record (GLONASS / SBAS): the state vector is already
            # at its own broadcast epoch, so record that epoch as GPS SoW and
            # leave the vector untouched.
            if e2.get("system") == "R":
                e2["toe_ref"] = _glonass_toe_ref(e2["toe_ref"], sow,
                                                 kepler_grid_s, keep_real_within_s)
            else:
                e2["toe_ref"] = sow
        elif (keep_real_within_s > 0
              and e2.get("system", "G") in _REAL_EPOCH_SYS
              and "gps_week" in e2
              and int(e2["gps_week"]) == int(week)   # geometry uses SoW only
              and abs((float(e2["gps_week"]) - week) * _WEEK_SECONDS
                      + float(e2["toe"]) - float(sow)) <= keep_real_within_s):
            e2["gps_week"] = int(e2["gps_week"])
        else:
            k = k_sow_bds if e2.get("system") == "C" else k_sow
            e2["toc"] = k
            e2["toe"] = k
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


def _pick_epoch(sub, noon_gps: float, sysc: str):
    """Select one broadcast record for a satellite, nearest the file's midday.

    Keplerian systems compare on absolute GPS time (week*604800 + toe) so a
    week rollover inside the file cannot select the wrong set; ECEF-state
    systems have no toe, so compare the record epoch itself.
    """
    if sysc in _KEPLER_SYS:
        best_i, best_d = 0, None
        for i in range(int(sub.time.size)):
            r = sub.isel(time=i)
            var, wk_off, t_off = _WEEK_VAR.get(sysc, ("GPSWeek", 0, 0.0))
            wk = float(r[var].values) + wk_off if var in r else 0.0
            toe = float(r["Toe"].values) + t_off if "Toe" in r else 0.0
            d = abs(wk * _WEEK_SECONDS + toe - noon_gps)
            if best_d is None or d < best_d:
                best_i, best_d = i, d
        return sub.isel(time=best_i)
    times = [_gps_seconds(_to_datetime(t)) for t in sub.time.values]
    best_i = min(range(len(times)), key=lambda i: abs(times[i] - noon_gps))
    return sub.isel(time=best_i)


_log = logging.getLogger(__name__)

# Valid raw-PRN ranges as they appear in a RINEX-3 nav file, BEFORE any
# ``_native_prn`` offset. Short forms for SBAS/QZSS; full-PRN forms are
# additionally accepted in ``_prn_in_range``.
_PRN_RANGE = {
    "G": (1, 32),
    "R": (1, 30),      # GLONASS slot numbers
    "E": (1, 50),      # Galileo E01..E36 today, headroom to 50
    "C": (1, 63),      # BeiDou C01..C63
    "J": (1, 10),      # QZSS short form J01..J07 (georinex), headroom to 10
    "S": (20, 58),     # SBAS short form S20..S58 == PRN 120..158
    "I": (1, 14),      # NavIC (IRNSS) I01..I14
}


def _prn_in_range(s: str, prn: int) -> bool:
    """True if ``prn`` is a plausible raw PRN for system letter ``s``.

    Accepts the short form for every system, plus the full-PRN form for
    SBAS (120..158) and QZSS (183..202) which some files emit instead.
    Unknown system letters are passed through (True).
    """
    rng = _PRN_RANGE.get(s)
    if rng is None:
        return True
    if rng[0] <= prn <= rng[1]:
        return True
    if s == "S" and 120 <= prn <= 158:
        return True
    if s == "J" and 183 <= prn <= 202:
        return True
    return False


_PARSE_CACHE: dict = {}
_PARSE_CACHE_MAX = 4


def parse_rinex_multi(path: str | pathlib.Path, systems=("G",),
                      require=None, at_gps: dt.datetime | None = None) -> dict:
    """Cached front of :func:`_parse_rinex_multi` (same contract).

    ``at_gps`` (GPS-time naive datetime): pick each satellite's broadcast
    record nearest that epoch instead of nearest the file's midday.

    A daily multi-GNSS BRDC file takes ~0.8 s of pure-Python (GIL-holding)
    georinex/xarray work to parse. A live session generates a 1 s segment
    every second, so re-parsing per segment both ate most of the real-time
    budget and starved the TX pump thread of the GIL (hardware underruns).
    Keyed on the file's identity and mtime, so an edited or re-downloaded
    file is re-parsed; records are copied out so callers may mutate them.
    """
    try:
        st = pathlib.Path(path).stat()
        key = (str(pathlib.Path(path).resolve()), st.st_mtime_ns, st.st_size,
               tuple(systems),
               None if require is None else tuple(require),
               None if at_gps is None else at_gps.isoformat())
    except OSError:
        return _parse_rinex_multi(path, systems, require, at_gps)
    hit = _PARSE_CACHE.get(key)
    if hit is None:
        hit = _parse_rinex_multi(path, systems, require, at_gps)
        if len(_PARSE_CACHE) >= _PARSE_CACHE_MAX:
            _PARSE_CACHE.pop(next(iter(_PARSE_CACHE)))
        _PARSE_CACHE[key] = hit
    return {k: dict(v) for k, v in hit.items()}


_ZERO_FIELD = " 0.000000000000e+00"


def _georinex_source(path):
    """What to hand ``georinex.load`` for ``path``.

    georinex parses each 19-column data field with ``float(field or 0)``:
    an empty field reads as 0, but a field of blanks raises, and the whole
    record is then silently discarded ("malformed line", all-NaN row, later
    dropped). Real-time BRDC files (BKG ConvertoCpp) write the spare fields
    at the end of a record as blanks, so every fresh record of the day was
    lost and only yesterday's -- hours stale -- survived. Blank data fields
    on continuation lines are rewritten as zeros (the value RINEX assigns a
    spare/unknown field); the original path is returned when nothing needs
    changing."""
    import io
    p = pathlib.Path(path)
    opener = gzip.open if p.suffix == ".gz" else open
    try:
        with opener(p, "rt", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return str(p)
    try:
        hdr_end = next(i for i, ln in enumerate(lines) if "END OF HEADER" in ln)
        version = float(lines[0][:9])
    except (StopIteration, ValueError):
        return str(p)
    col0 = 4 if version >= 3 else 3
    lead = " " * col0
    changed = False
    for i in range(hdr_end + 1, len(lines)):
        ln = lines[i]
        if not ln.startswith(lead) or not ln.strip():
            continue
        ln = ln.ljust(col0 + 4 * 19)
        fields = [ln[col0 + 19 * k: col0 + 19 * (k + 1)] for k in range(4)]
        if not any(not f.strip() for f in fields):
            continue
        lines[i] = lead + "".join(f if f.strip() else _ZERO_FIELD for f in fields)
        changed = True
    if not changed:
        return str(p)
    return io.StringIO("\n".join(lines) + "\n")


def _parse_rinex_multi(path: str | pathlib.Path, systems=("G",),
                       require=None, at_gps: dt.datetime | None = None) -> dict:
    """Parse a RINEX 2/3 nav file into per-satellite broadcast records.

    ``systems`` is an ordered iterable of RINEX system letters. When it is
    exactly ``("G",)`` the result is keyed by bare ``int`` PRN (back-compat
    with :func:`parse_rinex`); otherwise it is keyed by ``(sys_char, prn)``.
    Keplerian systems (G J E C) carry the ``_VARMAP_KEPLER`` fields plus
    ``toc``; ECEF-state systems (R S) carry the ``_VARMAP_STATE`` fields plus
    ``toe_ref``. Every record also carries ``"system"`` and ``"prn"``.

    ``require`` controls the absent-system policy. ``None`` (default): every
    letter in ``systems`` must be present or :class:`EphemerisUnavailable`
    is raised naming the missing ones. A tuple: only those letters are
    mandatory -- other requested-but-absent systems are simply not in the
    result, and the caller compares the keys it got against what it asked
    for to surface a warning instead of failing the whole run. Common daily
    mixed BRDC files often omit SBAS ('S') and are sparse in QZSS ('J').
    """
    systems = tuple(dict.fromkeys(systems))          # dedupe, keep order
    nav = gr.load(_georinex_source(path), use=list(systems))
    # File's calendar day -> that day's noon UTC, as continuous GPS seconds.
    mid_day = _to_datetime(nav.time.values[len(nav.time) // 2])
    noon = dt.datetime(mid_day.year, mid_day.month, mid_day.day, 12, 0, 0)
    noon_gps = _gps_seconds(noon) if at_gps is None else _gps_seconds(at_gps)
    out: dict = {}
    skipped: list[str] = []
    for sv in nav.sv.values:
        s = str(sv)[0]
        if s not in systems:
            continue
        prn = int(str(sv)[1:])
        if not _prn_in_range(s, prn):
            skipped.append(f"{s}{prn}")
            continue
        sub = nav.sel(sv=sv).dropna(dim="time", how="all")
        if sub.time.size == 0:
            continue
        rec = _pick_epoch(sub, noon_gps, s)
        e: dict = {"system": s, "prn": prn}
        vmap = (_VARMAP_BDS if s == "C" else _VARMAP_GAL if s == "E" else
                _VARMAP_KEPLER if s in _KEPLER_SYS else _VARMAP_STATE)
        for key, var in vmap.items():
            if var is None or var not in rec:
                continue
            e[key] = float(rec[var].values)
        epoch_sow = _seconds_of_week(_to_datetime(rec.time.values))
        if s in _KEPLER_SYS:
            # toc is the record's own clock reference epoch (distinct from toe).
            e["toc"] = epoch_sow
        else:
            e["toe_ref"] = epoch_sow
        out[prn if systems == ("G",) else (s, prn)] = e
    if skipped:
        _log.warning(
            "parse_rinex_multi: dropped %d out-of-range PRN record(s) from %s "
            "(e.g. %s)", len(skipped), path, ", ".join(skipped[:6]))
    if not out:
        raise EphemerisUnavailable(
            f"no ephemeris for systems {list(systems)!r} in {path}")
    missing = [s for s in systems
               if not any((k[0] if isinstance(k, tuple) else "G") == s
                          for k in out)]
    mandatory = list(systems) if require is None else list(require)
    hard_missing = [s for s in missing if s in mandatory]
    if hard_missing:
        raise EphemerisUnavailable(
            f"systems {hard_missing!r} not present in {path}")
    return out


def parse_rinex(path: str | pathlib.Path) -> dict[int, dict]:
    """GPS-only facade over :func:`parse_rinex_multi` -- output unchanged."""
    return parse_rinex_multi(path, ("G",))


def _hdr_floats(s: str) -> list[float]:
    import re

    out = []
    for tok in re.findall(r"[+-]?\d+\.?\d*(?:[DdEe][+-]?\d+)?", s):
        try:
            out.append(float(tok.replace("D", "E").replace("d", "E")))
        except ValueError:
            pass
    return out


def rinex_header_iono_utc(path: str | pathlib.Path) -> dict:
    """Best-effort Klobuchar (GPSA/GPSB or RINEX-2 ION ALPHA/BETA) and UTC
    (GPUT / DELTA-UTC) parameters from a nav-file header. Returns ``{}`` when
    the header carries none. Used to fill LNAV subframe 4 page 18."""
    out: dict = {}
    try:
        with open(path) as fh:
            for line in fh:
                label = line[60:].strip()
                if "END OF HEADER" in line:
                    break
                head = line[:60]
                tag4 = line[:4].strip().upper()
                if label == "ION ALPHA" or tag4 == "GPSA":
                    nums = _hdr_floats(head[4:] if tag4 == "GPSA" else head)
                    if len(nums) >= 4:
                        out["iono_alpha"] = nums[:4]
                elif label == "ION BETA" or tag4 == "GPSB":
                    nums = _hdr_floats(head[4:] if tag4 == "GPSB" else head)
                    if len(nums) >= 4:
                        out["iono_beta"] = nums[:4]
                elif label.startswith("DELTA-UTC") or tag4 == "GPUT":
                    nums = _hdr_floats(head[4:] if tag4 == "GPUT" else head)
                    if len(nums) >= 2:
                        utc = out.setdefault("utc", {})
                        utc["A0"], utc["A1"] = nums[0], nums[1]
                        if len(nums) >= 4:
                            utc["tot"], utc["WNt"] = int(nums[2]), int(nums[3])
    except OSError:
        pass
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


_TODAY_REFRESH_S = 1800.0


def get_ephemeris(date: dt.date, download: bool = True) -> dict[int, dict]:
    p = _cache_path(date)
    if (p.exists() and download
            and date == dt.datetime.utcnow().date()
            and time.time() - p.stat().st_mtime > _TODAY_REFRESH_S):
        # Today's BRDC grows all day (hourly uploads); a copy cached this
        # morning lacks the records a run starting "now" needs, and the
        # run would fall back to relabeled stale ones.
        fresh = _download(date)
        if fresh is not None:
            p = fresh
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
