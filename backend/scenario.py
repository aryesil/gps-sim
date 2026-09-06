from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from backend import config
from backend.synth import signals


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

    def __post_init__(self):
        self.systems = _norm_systems(self.systems)


def _bytes_per_sample(fmt: str) -> int:
    return 1 if fmt == "int8" else 2   # int12 is carried in an int16 container


def estimate_bytes(req: ScenarioRequest) -> int:
    return int(2 * _bytes_per_sample(req.sample_format) * req.sample_rate * req.duration_s)


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


def write_motion_csv(req: ScenarioRequest, path) -> None:
    if not req.route or len(req.route) < 2:
        raise ValueError("route needs at least two waypoints")
    n = req.duration_s * 10
    wp = req.route
    seg = len(wp) - 1
    lines = []
    for k in range(n):
        t = k / 10.0
        f = k / max(n - 1, 1) * seg
        i = min(int(f), seg - 1)
        frac = f - i
        a, b = wp[i], wp[i + 1]
        lat = a[0] + (b[0] - a[0]) * frac
        lon = a[1] + (b[1] - a[1]) * frac
        alt = a[2] + (b[2] - a[2]) * frac
        lines.append(f"{t:.1f},{lat:.9f},{lon:.9f},{alt:.3f}")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
