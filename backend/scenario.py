from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from backend import config


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


def _bytes_per_sample(fmt: str) -> int:
    return 1 if fmt == "int8" else 2


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
        # gps-sdr-sim -t/-T are GPS timescale; shift UTC start by GPS-UTC.
        # -T (not -t) also overwrites the nav file's TOC/TOE to this start
        # time instead of rejecting it as outside the file's validity window
        # -- our nav file (generator.run re-serializes via
        # ephemeris.to_rinex2_nav) carries only a single epoch per satellite,
        # so with -t any start more than an instant from that epoch would be
        # rejected ("Invalid start time"). Accuracy still degrades with
        # distance from the real epoch -- that's what the toe +/- 2h preview
        # warning in app.py is for.
        "-T", (req.start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)).strftime("%Y/%m/%d,%H:%M:%S"),
    ]
    if not req.ionosphere:
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
