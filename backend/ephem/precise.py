# backend/precise.py
"""Precise GNSS ephemeris: SP3 orbit/clock products as satellite state.

Scope (see docs/precise-ephemeris-design.md, Strategy D)
------------------------------------------------------
This module turns an IGS SP3-c/d precise orbit file into
``SatelliteState`` values -- ECEF position, velocity and (coarse) clock --
at an arbitrary epoch inside the product's coverage. It is used for
*analysis and verification*: comparing the broadcast / realigned ephemeris
that actually drives the generated IQ against a precise reference.

It does NOT feed signal generation. gps-sdr-sim consumes broadcast
Keplerian nav only; nothing here reaches the IQ.

What is and isn't modelled
--------------------------
SUPPORTED            satellite ECEF position (SP3 + ~10th-order Lagrange
                     interpolation, ~cm mid-arc)
SUPPORTED            satellite ECEF velocity (analytic derivative of the
                     same interpolating polynomial, ~mm/s)
PARTIALLY SUPPORTED  satellite clock bias/drift -- SP3 carries a coarse
                     (product-interval, ~us) clock; linearly interpolated
                     here. Not a 30 s CLK-RINEX precise clock.
NOT MODELLED         centre-of-mass -> antenna-phase-centre offset (SP3 is
                     CoM; broadcast is APC -- a ~cm-level radial bias),
                     relativistic clock correction beyond what SP3 already
                     includes, group delay (TGD), ionosphere, troposphere.
"""
from __future__ import annotations

import math
import pathlib
import re
from dataclasses import dataclass, field

from backend.gpstime import GPSTime, WEEK_SECONDS

# SP3 "bad or absent" sentinels.
_SP3_BAD_POS_KM = 0.0            # an all-zero position row = no data for that SV/epoch
_SP3_BAD_CLOCK_US = 999999.999999


class PreciseProductError(Exception):
    """Base for every precise-ephemeris failure."""


class PreciseProductParseError(PreciseProductError):
    """The SP3 text could not be parsed."""


class EpochOutOfCoverage(PreciseProductError):
    """Requested epoch is outside the product's time span (no extrapolation)."""


class InterpolationWindowError(PreciseProductError):
    """Epoch is inside coverage but too close to a file boundary to build the
    requested interpolation order, and reduced order was not allowed."""


class SatelliteNotInProduct(PreciseProductError):
    """The product carries no usable samples for the requested PRN."""


class PreciseClockUnavailable(PreciseProductError):
    """Orbit is available but the bracketing clock samples are flagged bad."""


@dataclass(frozen=True)
class SatelliteState:
    """A precise satellite state at one epoch. Units are explicit."""

    prn: int
    epoch: GPSTime
    position_ecef_m: tuple[float, float, float]
    velocity_ecef_mps: tuple[float, float, float]
    clock_bias_s: float
    clock_drift_sps: float
    source: str
    valid_from: GPSTime
    valid_to: GPSTime
    system: str = "G"

    def as_dict(self) -> dict:
        return {
            "prn": self.prn,
            "epoch_sow": self.epoch.sow,
            "epoch_week": self.epoch.week,
            "position_ecef_m": list(self.position_ecef_m),
            "velocity_ecef_mps": list(self.velocity_ecef_mps),
            "clock_bias_s": self.clock_bias_s,
            "clock_drift_sps": self.clock_drift_sps,
            "source": self.source,
            "valid_from_sow": self.valid_from.sow,
            "valid_to_sow": self.valid_to.sow,
            "system": self.system,
        }


# ---------------------------------------------------------------------------
# Neville interpolation with analytic first derivative.
# ---------------------------------------------------------------------------
def _neville_clean(xs: list[float], ys: list[float], x: float) -> tuple[float, float]:
    """Return (p(x), p'(x)) for the polynomial through (xs, ys).

    Neville's tableau carried alongside its analytic derivative (the same
    recurrence, differentiated once w.r.t. x). ``xs`` need not be equally
    spaced. Used with x shifted so the target sits near 0, which keeps the
    tableau well conditioned for a ~10th-order window.
    """
    n = len(xs)
    if n != len(ys) or n == 0:
        raise ValueError("xs and ys must be non-empty and the same length")
    P = list(ys)
    D = [0.0] * n
    for m in range(1, n):
        for i in range(n - m):
            xl, xr = xs[i], xs[i + m]
            denom = xl - xr
            Pnew = ((x - xr) * P[i] - (x - xl) * P[i + 1]) / denom
            Dnew = ((P[i] - P[i + 1]) + (x - xr) * D[i] - (x - xl) * D[i + 1]) / denom
            P[i], D[i] = Pnew, Dnew
    return P[0], D[0]


@dataclass
class SP3Product:
    """Parsed SP3 orbit/clock file."""

    source: str
    gps_week: int
    epoch_interval_s: float
    # (sys, prn) -> list of (t_seconds_since_gps_epoch, x_m, y_m, z_m, clk_s|nan), sorted
    records: dict[tuple, list[tuple[float, float, float, float, float]]] = field(default_factory=dict)
    epoch_times: list[float] = field(default_factory=list)  # union of epoch stamps, seconds

    # --- coverage ----------------------------------------------------
    @property
    def coverage_seconds(self) -> tuple[float, float]:
        if not self.epoch_times:
            raise PreciseProductParseError("SP3 product has no epochs")
        return self.epoch_times[0], self.epoch_times[-1]

    def coverage_gpstime(self) -> tuple[GPSTime, GPSTime]:
        lo, hi = self.coverage_seconds
        return GPSTime.from_seconds(lo), GPSTime.from_seconds(hi)

    def satellites(self) -> list[tuple]:
        return sorted(k for k, rows in self.records.items() if rows)

    def systems(self) -> list[str]:
        return sorted({k[0] for k in self.records})

    def available_epochs(self) -> list[GPSTime]:
        return [GPSTime.from_seconds(t) for t in self.epoch_times]


def _parse_sp3_epoch_line(line: str) -> float | None:
    """'*  2023 12 31 23 45  0.00000000' -> seconds since GPS epoch, or None."""
    parts = line[1:].split()
    if len(parts) < 6:
        return None
    try:
        y, mo, d, h, mi = (int(parts[i]) for i in range(5))
        s = float(parts[5])
    except ValueError:
        return None
    import datetime as _dt

    base = _dt.datetime(y, mo, d, h, mi) + _dt.timedelta(seconds=s)
    # SP3 epoch stamps are GPS time (no leap-second step from the epoch).
    return GPSTime.from_gps_datetime(base).seconds


def parse_sp3(text_or_path: str | pathlib.Path, *, source: str | None = None) -> SP3Product:
    """Parse SP3-c / SP3-d position (and optional clock) records.

    Velocity records ('V...') are ignored -- velocity is recovered by
    differentiating the position interpolant, which is what IGS recommends
    and avoids depending on files that omit the V block.
    """
    raw: str
    if isinstance(text_or_path, pathlib.Path) or (
        isinstance(text_or_path, str) and "\n" not in text_or_path and len(text_or_path) < 4096
        and pathlib.Path(text_or_path).exists()
    ):
        p = pathlib.Path(text_or_path)
        raw = p.read_text()
        source = source or p.name
    else:
        raw = str(text_or_path)
        source = source or "<memory>"

    lines = raw.splitlines()
    if not lines or lines[0][:1] != "#":
        raise PreciseProductParseError("not an SP3 file (missing '#' header)")

    gps_week = 0
    interval_s = 0.0
    records: dict[tuple, list] = {}
    epoch_times: list[float] = []
    cur_t: float | None = None
    seen_any_epoch = False

    for line in lines:
        tag = line[:2]
        if tag == "##":
            parts = line.split()
            try:
                gps_week = int(parts[1])
                interval_s = float(parts[3])
            except (IndexError, ValueError):
                pass
            continue
        if line[:1] == "*":
            cur_t = _parse_sp3_epoch_line(line)
            if cur_t is None:
                raise PreciseProductParseError(f"bad epoch line: {line!r}")
            epoch_times.append(cur_t)
            seen_any_epoch = True
            continue
        if line[:1] == "P":
            if cur_t is None:
                raise PreciseProductParseError("position record before any epoch line")
            sv = line[1:4].strip()
            sysc = sv[0]
            if sysc not in ("G", "R", "E", "C", "J"):
                continue  # drop SBAS and other systems
            try:
                prn = int(sv[1:])
                x_km = float(line[4:18])
                y_km = float(line[18:32])
                z_km = float(line[32:46])
                clk_us = float(line[46:60]) if len(line) >= 60 else _SP3_BAD_CLOCK_US
            except ValueError as ex:
                raise PreciseProductParseError(f"bad P record: {line!r} ({ex})") from ex
            if x_km == 0.0 and y_km == 0.0 and z_km == 0.0:
                continue  # SP3 "no data" sentinel for this SV at this epoch
            clk_s = math.nan if abs(clk_us) >= _SP3_BAD_CLOCK_US else clk_us * 1e-6
            records.setdefault((sysc, prn), []).append(
                (cur_t, x_km * 1e3, y_km * 1e3, z_km * 1e3, clk_s)
            )
            continue
        # 'V' velocity rows, 'EOF', '+', '%', '/*' comments: ignored.

    if not seen_any_epoch or not records:
        raise PreciseProductParseError("SP3 file carries no usable position records")

    for k in records:
        records[k].sort(key=lambda r: r[0])
    epoch_times = sorted(set(epoch_times))
    return SP3Product(source=source, gps_week=gps_week,
                      epoch_interval_s=interval_s, records=records,
                      epoch_times=epoch_times)


def merge_sp3(products: "list[SP3Product]") -> "SP3Product":
    """Concatenate several SP3 products (typically consecutive days) into
    one, so an interpolation window near a single file's edge can still be
    centred. Rows are unioned per (sys, PRN), sorted by epoch, and de-duplicated
    on the epoch stamp (first product wins a tie). Raises ValueError on an
    empty list.
    """
    products = [p for p in products if p is not None]
    if not products:
        raise ValueError("merge_sp3: nothing to merge")
    if len(products) == 1:
        return products[0]
    merged: dict[tuple, list] = {}
    for p in products:
        for key, rows in p.records.items():
            merged.setdefault(key, []).extend(rows)
    for key, rows in merged.items():
        rows.sort(key=lambda r: r[0])
        deduped, last_t = [], None
        for r in rows:
            if last_t is None or r[0] != last_t:
                deduped.append(r)
                last_t = r[0]
        merged[key] = deduped
    epoch_times = sorted({t for p in products for t in p.epoch_times})
    base = min(products, key=lambda p: p.epoch_times[0] if p.epoch_times else 0.0)
    srcs = "+".join(pathlib.Path(p.source).name for p in products)
    return SP3Product(source=f"merged({srcs})", gps_week=base.gps_week,
                      epoch_interval_s=base.epoch_interval_s,
                      records=merged, epoch_times=epoch_times)


class PreciseEphemerisProvider:
    """Serves ``SatelliteState`` from a loaded SP3 product.

    Nothing here does I/O beyond an explicit ``load()`` / ``load_text()``;
    the normal test suite never touches the network.
    """

    DEFAULT_ORDER = 10  # 11-point Lagrange window -- IGS-recommended for 15-min SP3

    def __init__(self) -> None:
        self._sp3: SP3Product | None = None

    # --- loading ---------------------------------------------------
    def load(self, path: str | pathlib.Path) -> SP3Product:
        self._sp3 = parse_sp3(pathlib.Path(path))
        return self._sp3

    def load_text(self, text: str, *, source: str = "<memory>") -> SP3Product:
        self._sp3 = parse_sp3(text, source=source)
        return self._sp3

    def set_product(self, sp3: SP3Product) -> None:
        self._sp3 = sp3

    @property
    def loaded(self) -> bool:
        return self._sp3 is not None

    @property
    def product(self) -> SP3Product:
        if self._sp3 is None:
            raise PreciseProductError("no precise product loaded")
        return self._sp3

    # --- queries ------------------------------------------------
    def available_epochs(self) -> list[GPSTime]:
        return self.product.available_epochs()

    def satellites(self) -> list[tuple]:
        return self.product.satellites()

    def coverage(self) -> tuple[GPSTime, GPSTime]:
        return self.product.coverage_gpstime()

    def status(self) -> dict:
        if self._sp3 is None:
            return {"loaded": False}
        lo, hi = self.product.coverage_gpstime()
        # For backwards compatibility, return just GPS PRNs as bare integers
        sats = self.satellites()
        gps_prns = sorted([prn for (sys, prn) in sats if sys == "G"])
        return {
            "loaded": True,
            "source": self.product.source,
            "gps_week": self.product.gps_week,
            "interval_s": self.product.epoch_interval_s,
            "satellites": gps_prns,
            "systems": self.product.systems(),
            "epochs": len(self.product.epoch_times),
            "coverage_start_utc": lo.to_datetime().isoformat() + "Z",
            "coverage_end_utc": hi.to_datetime().isoformat() + "Z",
        }

    # --- the core query ------------------------------------------
    def get_state(self, prn: int, epoch: GPSTime, *, order: int | None = None,
                  allow_boundary: bool = False,
                  allow_reduced_order: bool | None = None) -> SatelliteState:
        # allow_reduced_order kept as a back-compatible alias
        if allow_reduced_order is not None:
            allow_boundary = allow_boundary or allow_reduced_order
        sp3 = self.product
        # Normalise bare-int PRN to ("G", prn) tuple for backwards compatibility
        key = ("G", prn) if isinstance(prn, int) else tuple(prn)
        rows = sp3.records.get(key)
        if not rows:
            raise SatelliteNotInProduct(f"PRN {prn} not in {sp3.source}")

        t = epoch.seconds
        lo, hi = rows[0][0], rows[-1][0]
        if t < lo or t > hi:
            raise EpochOutOfCoverage(
                f"epoch {epoch.week}:{epoch.sow:.1f} outside PRN {prn} coverage "
                f"[{GPSTime.from_seconds(lo).sow:.1f} .. {GPSTime.from_seconds(hi).sow:.1f}] "
                f"of {sp3.source}")

        want = (self.DEFAULT_ORDER if order is None else int(order)) + 1
        want = min(want, len(rows))
        # centre the window on the interval containing t
        import bisect

        ts = [r[0] for r in rows]
        j = bisect.bisect_right(ts, t) - 1
        j = max(0, min(j, len(rows) - 2))
        half = want // 2
        start = j - half + 1
        end = start + want
        centred = 0 <= start and end <= len(rows)
        if not centred and not allow_boundary:
            raise InterpolationWindowError(
                f"epoch is within coverage but less than {half} samples from a "
                f"boundary of {sp3.source}; a centred order-{want - 1} window is "
                f"not available. Pass allow_boundary=True to accept an "
                f"off-centre (or, for a very short file, lower-order) window.")
        start = max(0, min(start, len(rows) - want))
        end = start + want
        win = rows[start:end]

        wt = [r[0] - t for r in win]  # shift so x ~ 0: better conditioned
        px, vx = _neville_clean(wt, [r[1] for r in win], 0.0)
        py, vy = _neville_clean(wt, [r[2] for r in win], 0.0)
        pz, vz = _neville_clean(wt, [r[3] for r in win], 0.0)

        clk_bias, clk_drift = _interp_clock(rows, ts, j, t, prn, sp3.source)

        vf = GPSTime.from_seconds(win[0][0])
        vt = GPSTime.from_seconds(win[-1][0])
        tag = f"lagrange{len(win) - 1}"
        if len(win) < self.DEFAULT_ORDER + 1 and (order is None):
            tag += "(reduced)"
        elif not centred:
            tag += "(offcentre)"
        src = f"precise:{sp3.source} {tag}"
        return SatelliteState(
            prn=prn, epoch=epoch,
            position_ecef_m=(px, py, pz),
            velocity_ecef_mps=(vx, vy, vz),
            clock_bias_s=clk_bias, clock_drift_sps=clk_drift,
            source=src, valid_from=vf, valid_to=vt,
            system=key[0])

    def state_fn(self, prn: int, *, week: int | None = None,
                 order: int | None = None, allow_boundary: bool = False):
        """Return ``f(sow_seconds) -> (pos, vel, clk_bias)`` for geometry.py.

        ``geometry.observables`` iterates the transmit-time solution in
        seconds-of-week, calling this at several nearby epochs -- exactly
        the arbitrary-epoch query precise interpolation is for. ``week`` is
        the GPS week those seconds-of-week belong to; it defaults to the
        product's own week, but the caller should pass the scenario's true
        week so a product-week mismatch surfaces as EpochOutOfCoverage
        rather than a silently wrong interpolation.
        """
        wk = self.product.gps_week if week is None else int(week)
        # Normalise bare-int PRN to ("G", prn) tuple for backwards compatibility
        key = ("G", prn) if isinstance(prn, int) else tuple(prn)

        def f(sow_seconds: float):
            epoch = GPSTime(wk, sow_seconds)
            st = self.get_state(key, epoch, order=order,
                                allow_boundary=allow_boundary)
            return (list(st.position_ecef_m),
                    list(st.velocity_ecef_mps),
                    st.clock_bias_s)

        return f


_SP3_SYS_LETTERS = "GRECJ"


def _sp3_systems(data: bytes) -> set[str]:
    """Return the set of GNSS system letters an SP3 blob carries.

    Cheap and format-tolerant (SP3-a/c/d): scan the ``+`` satellite-id
    header block first, then the ``P`` position records. Any letter in
    ``GRECJ`` counts; SBAS ('S') and unknown letters are ignored.
    """
    out: set[str] = set()
    try:
        text = data.decode("latin-1", "replace")
    except Exception:                              # pragma: no cover - defensive
        return out
    for line in text.splitlines():
        head2 = line[:2]
        if head2 == "+ ":                          # '+   10   G01G02...'
            for m in re.finditer(r"([GRECJ])\d{2}", line):
                out.add(m.group(1))
        elif line[:1] == "P" and len(line) >= 4:
            c = line[1]
            if c in _SP3_SYS_LETTERS:
                out.add(c)
        elif line[:1] == "*" and out:
            # header sat-id block is done and it already told us the
            # systems; no need to walk every P record of a big file.
            break
    return out


def _cov_tag(systems: set[str]) -> str:
    """Cache-name tag listing the systems a product really carries, in
    ``GRECJ`` order (``"G"``, ``"GR"``, ``"GRE"``, ``"GRECJ"``...). Before
    this, any non-GPS letter tagged the file ``GRECJ``, so a GPS+GLONASS-only
    product (ESA's operational orbits) passed as a full multi-GNSS one and
    Galileo/BeiDou were silently dropped."""
    return "".join(c for c in _SP3_SYS_LETTERS if c in systems) or "G"


def _wanted(want_systems, want_multignss: bool) -> set[str]:
    """Systems a product must carry. Only letters an SP3 can hold count."""
    if want_systems:
        w = {s for s in want_systems if s in _SP3_SYS_LETTERS}
        return w | {"G"}
    return set(_SP3_SYS_LETTERS) if want_multignss else {"G"}


# Hosts that refused or timed out a connection recently: host -> retry-after
# (time.monotonic()). An unreachable mirror (igs.ign.fr from many networks)
# otherwise cost a 30 s connect timeout per URL, per day, per request.
_DEAD_HOSTS: dict[str, float] = {}
_DEAD_FOR_S = 600.0
_CONNECT_TIMEOUT_S = 6.0
_READ_TIMEOUT_S = 60.0


class _FetchError(Exception):
    pass


def _host(url: str) -> str:
    from urllib.parse import urlsplit
    return urlsplit(url).netloc


def _fetch(url: str) -> bytes:
    """GET ``url`` (http/https via requests, ftp via urllib). Raises
    _FetchError; a connection failure also marks the host dead for a while
    so the remaining URLs on it are skipped."""
    import time as _t

    host = _host(url)
    if _DEAD_HOSTS.get(host, 0.0) > _t.monotonic():
        raise _FetchError(f"{host} unreachable (skipped)")
    if url.startswith("ftp://"):
        import socket
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(url, timeout=_CONNECT_TIMEOUT_S * 2) as r:
                return r.read()
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, (socket.timeout, TimeoutError,
                                   ConnectionError, OSError)) and \
                    "550" not in str(reason):
                _DEAD_HOSTS[host] = _t.monotonic() + _DEAD_FOR_S
            raise _FetchError(f"{url}: {reason}") from None
        except (OSError, EOFError) as e:
            _DEAD_HOSTS[host] = _t.monotonic() + _DEAD_FOR_S
            raise _FetchError(f"{url}: {e}") from None
    import requests as _rq
    try:
        r = _rq.get(url, timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S))
        r.raise_for_status()
    except (_rq.ConnectionError, _rq.Timeout) as e:
        _DEAD_HOSTS[host] = _t.monotonic() + _DEAD_FOR_S
        raise _FetchError(f"{url}: {e}") from None
    except _rq.RequestException as e:
        raise _FetchError(f"{url}: {e}") from None
    return r.content


def _sp3_payload(url: str) -> bytes:
    """Fetch and gunzip one SP3 product; raises _FetchError if it is not
    SP3."""
    import gzip as _gz

    data = _fetch(url)
    if data[:2] == b"\x1f\x8b":
        data = _gz.decompress(data)
    if data[:2] not in (b"#c", b"#d", b"#a", b"#b"):
        raise _FetchError(f"{url}: not an SP3 file")
    return data


def download_sp3(gps_week: int, dow: int, cache_dir, mirrors: list[str],
                 *, want_multignss: bool = False,
                 want_systems=None) -> str:
    """Best-effort SP3 fetch for one GPS day. Disabled unless ``mirrors`` is
    non-empty (config.PRECISE_SP3_MIRRORS). Mirror templates (http, https
    or ftp) may use any of ``{gpsweek}`` / ``{gps_week}`` (4-digit GPS
    week), ``{dow}`` (day of week 0-6), ``{yyyy}`` (calendar year), ``{doy}``
    (3-digit day of year), ``{wwwwd}`` (GPS week + dow, the legacy
    short-name stem) and ``{hh}`` (ultra-rapid solution hour, always
    ``"00"`` here -- the 00:00 solution's 2-day span covers the whole day).

    ``want_systems`` (or ``want_multignss`` = all of GRECJ) names the
    systems the product must carry: mirrors are probed in order until one
    covers them all, else the product covering the most of them is
    returned. Returns the cached local path. Raises PreciseProductError if
    no mirror yields a plausible SP3 file -- the caller never gets a stale
    or unrelated file.
    """
    import datetime as _dt
    import pathlib as _pl

    _d = _dt.date(1980, 1, 6) + _dt.timedelta(days=gps_week * 7 + dow)
    _doy = _d.timetuple().tm_yday
    _fmt = dict(gpsweek=gps_week, gps_week=gps_week, dow=dow,
                yyyy=_d.year, doy=f"{_doy:03d}", wwwwd=f"{gps_week:04d}{dow}",
                hh="00")

    if not mirrors:
        raise PreciseProductError(
            "SP3 download requested but PRECISE_SP3_MIRRORS is not configured")
    want = _wanted(want_systems, want_multignss)
    cache = _pl.Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)

    def _tier(s: str) -> str:
        s = s.upper()
        if "FIN" in s or "_R_" in s:
            return "FIN"
        if "ULT" in s or "IGU" in s:
            return "ULT"
        return "RAP"

    stem = f"IGS_{gps_week:04d}_{dow}"

    def _systems_of(path) -> set[str]:
        # Read the content, never trust the name: pre-fix caches were
        # tagged _GRECJ while holding GPS+GLONASS only.
        try:
            return _sp3_systems(path.read_bytes())
        except OSError:
            return set()

    # Serve the best already-cached product for this day: full coverage of
    # the wanted systems first, then tier (final > rapid > ultra-rapid).
    best_cached = None
    for tag in ("FIN", "RAP", "ULT"):
        for p in sorted(cache.glob(f"{stem}_{tag}*.sp3")) + (
                [cache / f"{stem}.sp3"] if tag == "RAP" else []):
            if not (p.is_file() and p.stat().st_size > 0):
                continue
            if want <= _systems_of(p):
                return str(p)
            if best_cached is None:
                best_cached = p

    last = None
    best: tuple[int, str] | None = None        # (wanted systems covered, path)
    for tmpl in mirrors:
        url = tmpl.format(**_fmt)
        try:
            data = _sp3_payload(url)
        except (_FetchError, OSError) as e:
            last = e
            continue
        systems = _sp3_systems(data)
        dest = cache / f"{stem}_{_tier(url)}_{_cov_tag(systems)}.sp3"
        dest.write_bytes(data)
        if want <= systems:
            return str(dest)
        score = len(want & systems)
        if best is None or score > best[0]:
            best = (score, str(dest))
    if best is not None:
        return best[1]
    if best_cached is not None:
        return str(best_cached)
    raise PreciseProductError(f"all SP3 mirrors failed ({last})")


def download_sp3_ultra(t_gps_s: float, cache_dir, mirrors: list[str], *,
                       want_systems=None, max_age_h: float = 46.0,
                       lead_s: float = 10800.0) -> str:
    """Newest ultra-rapid product whose 2-day span covers ``t_gps_s``
    (continuous GPS seconds) with interpolation margin -- for epochs too
    recent for any rapid product (today, and usually yesterday). Its first
    day is estimated, its second predicted.

    ``mirrors`` templates take ``{gpsweek}``, ``{dow}``, ``{yyyy}``,
    ``{doy}`` and ``{hh}`` of the solution's first epoch; solutions are
    looked for on a 3-hourly grid from ``t - lead_s`` back ``max_age_h``
    hours (the lead keeps the interpolation window centred). Each template's directory is listed once, so only files that
    exist are downloaded. Coverage of ``want_systems`` decides between
    mirrors as in :func:`download_sp3`."""
    import datetime as _dt
    import pathlib as _pl

    if not mirrors:
        raise PreciseProductError("no ultra-rapid SP3 mirrors configured")
    want = _wanted(want_systems, bool(want_systems))
    cache = _pl.Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    gps0 = _dt.datetime(1980, 1, 6)
    newest = int((t_gps_s - lead_s) // 10800) * 10800
    listings: dict[str, str | None] = {}

    def _listed(url: str) -> bool:
        d, name = url.rsplit("/", 1)
        if d not in listings:
            try:
                listings[d] = _fetch(d + "/").decode("latin-1", "replace")
            except (_FetchError, OSError):
                listings[d] = None
        text = listings[d]
        return text is None or name in text    # no listing: just try it

    best: tuple[int, str] | None = None
    last = None
    t = newest
    while t >= t_gps_s - max_age_h * 3600.0:
        ep = gps0 + _dt.timedelta(seconds=t)
        wk, rem = divmod(int(t), 604800)
        fmt = dict(gpsweek=wk, gps_week=wk, dow=rem // 86400, yyyy=ep.year,
                   doy=f"{ep.timetuple().tm_yday:03d}", hh=f"{ep.hour:02d}")
        for tmpl in mirrors:
            url = tmpl.format(**fmt)
            name = url.rsplit("/", 1)[1]
            dest = cache / ("ULT_" + name.replace(".gz", "").replace(".GZ", ""))
            if dest.is_file() and dest.stat().st_size > 0:
                data = dest.read_bytes()
            else:
                if not _listed(url):
                    continue
                try:
                    data = _sp3_payload(url)
                except (_FetchError, OSError) as e:
                    last = e
                    continue
                dest.write_bytes(data)
            systems = _sp3_systems(data)
            if want <= systems:
                return str(dest)
            score = len(want & systems)
            if best is None or score > best[0]:
                best = (score, str(dest))
        if best is not None:
            # the newest solution that exists wins; an older one with more
            # systems is not worth a day-older prediction
            return best[1]
        t -= 10800
    raise PreciseProductError(
        f"no ultra-rapid SP3 covers this epoch ({last})")


def _interp_clock(rows, ts, j, t, prn, source) -> tuple[float, float]:
    """Linear clock interpolation between the two bracketing SP3 samples."""
    a = rows[j]
    b = rows[min(j + 1, len(rows) - 1)]
    ca, cb = a[4], b[4]
    if math.isnan(ca) or math.isnan(cb):
        raise PreciseClockUnavailable(
            f"PRN {prn} clock flagged bad around this epoch in {source}")
    if b[0] == a[0]:
        return ca, 0.0
    slope = (cb - ca) / (b[0] - a[0])
    bias = ca + slope * (t - a[0])
    return bias, slope
