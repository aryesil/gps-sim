from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
import re
import subprocess

from backend import config, ephemeris, scenario

_TIME_RE = re.compile(r"Time into run\s*=\s*([0-9.]+)")


class GeneratorError(Exception):
    pass


def parse_progress(line: str, duration_s: float) -> float | None:
    m = _TIME_RE.search(line)
    if not m or duration_s <= 0:
        return None
    return min(float(m.group(1)) / duration_s, 1.0)


def binary_version(binary: str | None = None) -> str:
    b = binary or config.GPS_SDR_SIM_BIN
    try:
        p = subprocess.run([b], capture_output=True, text=True, timeout=10)
        out = (p.stdout + p.stderr).strip().splitlines()
        return out[0] if out else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _prepare_nav(req: scenario.ScenarioRequest, outdir: pathlib.Path,
                 time_offset_s: float = 0.0) -> pathlib.Path:
    """Resolve + realign ephemeris for req.start (+ time_offset_s), write it
    as a RINEX-2 nav file inside outdir, and return its path. Shared by
    run() and run_segment() so the F2/F4 handling in KNOWN_ISSUES only
    exists once."""
    eph = ephemeris.parse_rinex(req.rinex_path)
    gps_start = (req.start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
                 + dt.timedelta(seconds=time_offset_s))
    week, sow = ephemeris.gps_week_and_sow(gps_start)
    eph = ephemeris.align_epochs(eph, week, sow)
    nav_path = outdir / "nav.rinex2.n"
    nav_path.write_text(ephemeris.to_rinex2_nav(eph))
    return nav_path


def _run_gps_sdr_sim(argv: list[str], duration_s: float, progress_cb=None) -> None:
    """Run gps-sdr-sim to completion, draining its merged stdout/stderr
    (KNOWN_ISSUES F5) and raising GeneratorError on nonzero exit. Shared
    exec core for run() and run_segment()."""
    tail_lines: list[str] = []
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        tail_lines.append(line)
        if len(tail_lines) > 100:
            tail_lines.pop(0)
        frac = parse_progress(line, duration_s)
        if frac is not None and progress_cb:
            progress_cb(frac)
    proc.wait()
    if proc.returncode != 0:
        tail = "".join(tail_lines)[-2000:]
        raise GeneratorError(f"gps-sdr-sim exit {proc.returncode}: {tail}")


def run(req: scenario.ScenarioRequest, progress_cb=None, binary: str | None = None) -> pathlib.Path:
    b = binary or config.GPS_SDR_SIM_BIN
    outdir = config.OUT_DIR / dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
    outdir.mkdir(parents=True, exist_ok=True)
    out_bin = outdir / "gpssim.bin"

    motion_csv = None
    if req.route:
        motion_csv = str(outdir / "motion.csv")
        scenario.write_motion_csv(req, motion_csv)

    argv = [b] + scenario.build_args(req, str(out_bin), motion_csv)
    nav_path = _prepare_nav(req, outdir)
    argv[argv.index("-e") + 1] = str(nav_path)
    _run_gps_sdr_sim(argv, req.duration_s, progress_cb)
    if progress_cb:
        progress_cb(1.0)

    meta = {
        "config": {
            "lat": req.lat, "lon": req.lon, "alt": req.alt,
            "start_utc": req.start.isoformat(), "duration_s": req.duration_s,
            "rinex_path": req.rinex_path, "route": req.route,
        },
        "argv": argv,
        "binary_version": binary_version(b),
        "sample_rate": req.sample_rate,
        "sample_format": req.sample_format,
        "created_utc": dt.datetime.utcnow().isoformat(),
        "output": "gpssim.bin",
    }
    (outdir / "meta.json").write_text(json.dumps(meta, indent=2))
    return outdir


def run_segment(base_req: scenario.ScenarioRequest, llh: tuple[float, float, float],
                time_offset_s: float, duration_s: float = 1.0,
                binary: str | None = None) -> pathlib.Path:
    """Short, static-position segment for LiveSession: same gps-sdr-sim
    invocation as run(), but with base_req's lat/lon/alt overridden by
    `llh` and the whole segment's GPS time shifted by `time_offset_s`
    (real GPS-time-of-week spoofing, not cosmetic). No route/motion_csv
    support: live segments are one static point per segment by construction.

    The shift is applied to seg_req.start, so BOTH the nav file's aligned
    toc/toe (_prepare_nav) and gps-sdr-sim's `-t` (scenario.build_args) move
    together. Shifting only the nav epoch would leave tk = t - toe nonzero
    and silently propagate every satellite to the wrong point in its orbit
    -- geometry corruption, not a time shift."""
    b = binary or config.GPS_SDR_SIM_BIN
    outdir = config.OUT_DIR / ("live_" + dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S%f"))
    outdir.mkdir(parents=True, exist_ok=True)
    out_bin = outdir / "gpssim.bin"

    seg_req = dataclasses.replace(base_req, lat=llh[0], lon=llh[1], alt=llh[2],
                                  start=base_req.start + dt.timedelta(seconds=time_offset_s),
                                  duration_s=duration_s, route=None)
    argv = [b] + scenario.build_args(seg_req, str(out_bin), motion_csv=None)
    nav_path = _prepare_nav(seg_req, outdir, time_offset_s=0.0)
    argv[argv.index("-e") + 1] = str(nav_path)
    _run_gps_sdr_sim(argv, duration_s)
    return outdir
