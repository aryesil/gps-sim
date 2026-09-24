from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from backend import config
from backend.synth import bands as _bandsmod
from backend.synth import fs_policy, signals


def _norm_systems(systems) -> tuple:
    """Normalise a ``systems`` value to a sorted, unique tuple of RINEX system
    letters. Every letter must be in ``signals.SYSTEMS`` or ``ValueError``."""
    if systems is None:
        systems = ("G",)
    if isinstance(systems, str):
        systems = (systems,)
    out = tuple(sorted(set(systems)))
    if not out:
        raise ValueError("systems must be non-empty")
    bad = [s for s in out if s not in signals.SYSTEMS]
    if bad:
        raise ValueError(f"unknown systems {bad!r}; valid: {signals.SYSTEMS}")
    return out


@dataclass
class ScenarioRequest:
    rinex_path: str
    lat: float
    lon: float
    alt: float
    start: dt.datetime
    duration_s: int
    sample_rate: float = config.DEFAULT_SAMPLE_RATE
    sample_format: str = "int16"
    route: list[tuple[float, float, float]] | None = None
    ionosphere: bool = False
    # When set, this parsed-ephemeris dict (one entry per PRN, in the shape
    # ephemeris.parse_rinex returns) is written straight to the nav file
    # gps-sdr-sim reads, with NO toc/toe realignment. Used for precise
    # (SP3-fitted) generation -- the records already carry a real toe.
    nav_override: dict | None = None
    # Optional deterministic post-processing of the generated IQ. When set,
    # this dict is parsed by backend.models.impairments.ImpairmentConfig.from_dict
    # and applied to gpssim.bin after generation; the clean file is kept as
    # gpssim.clean.bin. Default None == no post-processing, byte-identical
    # output to before.
    impairments: dict | None = None
    random_seed: int | None = None
    # Optional physical-channel / receiver models. Each is a plain dict
    # parsed by the matching backend module's Config.from_dict and is
    # DISABLED unless it carries a non-"off" model. They feed the truth /
    # preview observables unconditionally; whether they also alter the
    # generated IQ is gated by ``models_to_iq`` (default False -> the IQ is
    # byte-identical to before). ``atmosphere.ionosphere == "klobuchar"``
    # additionally lets gps-sdr-sim apply its own broadcast Klobuchar
    # (the ``-i`` flag) so the ionosphere is present in the IQ too;
    # troposphere and multipath and receiver-clock effects reach the IQ
    # only through the opt-in post-processing stage.
    atmosphere: dict | None = None
    receiver_clock: dict | None = None
    multipath: dict | None = None
    models_to_iq: bool = False
    # Signal-generation engine. "gps-sdr-sim" (default, external binary) or
    # "native" (backend.synth, opt-in). Unknown values raise in signal_engine.
    engine: str = "gps-sdr-sim"
    # Deterministic per-SV fading, parsed by backend.synth.fading.FadingConfig.
    # None -> no fading (native engine emits static per-SV gain).
    fading: dict | None = None
    # GNSS systems to synthesize (native engine). Normalised in __post_init__ to
    # a sorted unique tuple of RINEX letters (subset of signals.SYSTEMS).
    systems: tuple = ("G",)
    # Modulate the IS-GPS-200 LNAV navigation message onto the native-engine
    # GPS signal (subframes 1-5, TLM/HOW, standard parity). False reproduces
    # the pre-SP-A output (constant data symbol). GPS only; other systems
    # keep a constant symbol until SP-D.
    nav_message: bool = True

    # Re-propagate every satellite's geometry per mixer block instead of
    # holding the run-start Doppler constant over the whole run. False falls
    # back to the Phase-1 constant-Doppler approximation.
    continuous_doppler: bool = True

    # RF bands to synthesise. None => L1 only (every legacy scenario is
    # byte-identical). A list like ["L1", "L2", "L5"] opts into extra bands;
    # l2_sample_rate / l5_sample_rate are optional per-band fs overrides
    # (floored by fs_policy.band_floor).
    bands: list[str] | None = None
    l2_sample_rate: float | None = None
    l5_sample_rate: float | None = None
    # Native engine: UTC epoch the broadcast ephemeris is aligned to
    # (ephemeris.align_epochs). None = ``start``. A live session pins it to
    # the SESSION start so every segment uses the same toe/orbit; aligning
    # per segment would relabel the orbit -- and the broadcast toe -- each
    # time a segment crosses a toe-grid boundary.
    eph_epoch: dt.datetime | None = None

    def __post_init__(self):
        self.systems = _norm_systems(self.systems)


def _bytes_per_sample(fmt: str) -> int:
    return 1 if fmt == "int8" else 2   # int12 is carried in an int16 container


def estimate_bytes(req: ScenarioRequest) -> int:
    """Bytes for the primary output plus any extra bands ``req.bands`` opts
    into. The primary term is the pre-existing estimate (``req.sample_rate``
    directly); each additional band gets its own file sized at its
    ``fs_policy`` floor (or its ``l{2,5}_sample_rate`` override), since an
    extra band's floor (L5 is >=25 Msps) can dwarf the primary band's rate --
    without this the disk-space guard below would pass on the primary band's
    few Msps alone and the run could still run out of space partway through.
    """
    bps = _bytes_per_sample(req.sample_format)
    total = int(2 * bps * req.sample_rate * req.duration_s)
    for band_id in (req.bands or []):
        if band_id == "L1":
            continue
        sig_ids = sorted({
            _bandsmod._signal_key(sig)
            for sysc in req.systems
            for sig in signals.signals_for(sysc, (band_id,))})
        if not sig_ids:
            continue
        override = getattr(req, f"{band_id.lower()}_sample_rate", None)
        fs = max(float(override or 0.0), fs_policy.band_floor(band_id, sig_ids))
        total += int(2 * bps * fs * req.duration_s)
    return total


def build_args(req: ScenarioRequest, out_bin: str, motion_csv: str | None) -> list[str]:
    # gps-sdr-sim argv (without the binary). Dynamic branch uses `-x` (user
    # motion in LLH lat,lon,height) to match write_motion_csv's geodetic output.
    args = [
        "-e", req.rinex_path,
        "-o", out_bin,
        "-s", str(req.sample_rate),
        "-b", "8" if req.sample_format == "int8" else "16",
        "-d", str(req.duration_s),
        # gps-sdr-sim -t is GPS timescale; shift UTC start by GPS-UTC.
        # generator.run (ephemeris.align_epochs) already overwrites every
        # satellite's toc/toe in the nav file to exactly this start, so -t's
        # strict validity-window check always passes; gps-sdr-sim's own -T
        # (which does the same realignment but only for the first satellite
        # it finds) is deliberately not used -- see KNOWN_ISSUES F4.
        # Accuracy still degrades with distance from the real epoch --
        # that's what the toe +/- 2h preview warning in app.py is for.
        "-t", (req.start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)).strftime("%Y/%m/%d,%H:%M:%S"),
    ]
    # gps-sdr-sim's -i flag *disables* its internal broadcast Klobuchar.
    # Keep it enabled (omit -i) when the legacy ionosphere bool is set, or
    # when the atmosphere model selects klobuchar and the operator asked
    # for models to reach the IQ.
    atmo = req.atmosphere or {}
    iono_in_iq = bool(req.ionosphere) or (
        req.models_to_iq and atmo.get("ionosphere") == "klobuchar")
    if not iono_in_iq:
        args.append("-i")
    if req.route:
        if motion_csv is None:
            raise ValueError("dynamic scenario needs motion_csv")
        args += ["-x", motion_csv]
    else:
        args += ["-l", f"{req.lat},{req.lon},{req.alt}"]
    return args


def route_llh_at(route, duration_s, t: float) -> tuple[float, float, float]:
    """Receiver ``(lat, lon, alt)`` at run time ``t`` seconds for a waypoint
    ``route``. Linear interpolation of the waypoints spread evenly across
    ``duration_s`` -- the same motion semantics ``gps-sdr-sim`` gets from the
    ``-x`` motion CSV. ``t`` outside ``[0, duration_s]`` clamps to the route
    endpoints. Shared by :func:`write_motion_csv` and the native engine's
    per-block geometry loop so the two never diverge.
    """
    if not route or len(route) < 2:
        raise ValueError("route needs at least two waypoints")
    seg = len(route) - 1
    n = int(round(duration_s * 10))
    k = t * 10.0
    f = (k / max(n - 1, 1)) * seg
    f = min(max(f, 0.0), float(seg))
    i = min(int(f), seg - 1)
    frac = f - i
    a, b = route[i], route[i + 1]
    return (a[0] + (b[0] - a[0]) * frac,
            a[1] + (b[1] - a[1]) * frac,
            a[2] + (b[2] - a[2]) * frac)


def write_motion_csv(req: ScenarioRequest, path) -> None:
    if not req.route or len(req.route) < 2:
        raise ValueError("route needs at least two waypoints")
    n = req.duration_s * 10
    lines = []
    for k in range(n):
        t = k / 10.0
        lat, lon, alt = route_llh_at(req.route, req.duration_s, t)
        lines.append(f"{t:.1f},{lat:.9f},{lon:.9f},{alt:.3f}")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
