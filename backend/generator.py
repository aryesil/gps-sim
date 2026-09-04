from __future__ import annotations

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
    # gps-sdr-sim's bundled parser only understands RINEX-2 nav (KNOWN_ISSUES
    # F2); re-serialize whatever RINEX version was resolved into one so it
    # always gets a file it accepts. Align every satellite's toc/toe to the
    # requested start ourselves (KNOWN_ISSUES F4) rather than relying on
    # gps-sdr-sim's own -T, which only realigns to whichever satellite it
    # finds first and aborts ("No current set of ephemerides has been
    # found") if the file's other satellites keep their own, possibly
    # far-off broadcast epochs.
    eph = ephemeris.parse_rinex(req.rinex_path)
    gps_start = req.start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    week, sow = ephemeris.gps_week_and_sow(gps_start)
    eph = ephemeris.align_epochs(eph, week, sow)
    nav_path = outdir / "nav.rinex2.n"
    nav_path.write_text(ephemeris.to_rinex2_nav(eph))
    argv[argv.index("-e") + 1] = str(nav_path)
    # gps-sdr-sim's progress lines ("Time into run = ...") print to stderr,
    # not stdout (confirmed by inspecting its output directly). Reading only
    # stdout while nobody drains stderr deadlocks as soon as stderr's ~3000
    # lines (one per 0.1s of a multi-minute scenario) fill the OS pipe
    # buffer: the child blocks on write(), the parent blocks on proc.wait()
    # -- the process never dies, generate never completes (KNOWN_ISSUES F5).
    # Merging stderr into stdout means there is only one pipe to drain.
    tail_lines: list[str] = []
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        tail_lines.append(line)
        if len(tail_lines) > 100:
            tail_lines.pop(0)
        frac = parse_progress(line, req.duration_s)
        if frac is not None and progress_cb:
            progress_cb(frac)
    proc.wait()
    if proc.returncode != 0:
        tail = "".join(tail_lines)[-2000:]
        raise GeneratorError(f"gps-sdr-sim exit {proc.returncode}: {tail}")
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
