# SPDX-License-Identifier: MIT
"""Golden vectors for the on-board GPSDO C daemon (gpsdod).

Evaluates the Python reference (``backend.rf.xo``, ``backend.rf.holdover`` and
``scripts.gpsdo_cycle.decision``) on fixed grids and writes the results as a C99 header that the
daemon's unit tests compile in and must reproduce within 1e-9 relative.

    PYTHONPATH=. python scripts/gen_gpsdo_vectors.py D:/codes/tezuka/app/gpsdod/test/vectors.h
"""
from __future__ import annotations

import sys
from pathlib import Path

from backend.rf.holdover import Holdover
from backend.rf.xo import split_osc_error
from scripts.gpsdo_cycle import decision

LAT = 39.9179
# The first osc value is the baseline measurement of the reference run (first row of XO_CASES).
OSC_PPM = [-3.5228, -3.60, -3.5, 0.0, 12.0]
E0_PPM = [-3.5228, -3.552, -3.570, -3.594, -3.60]
DURATIONS = [0, 30, 120, 320, 900]            # 0 is the Taylor-at-start branch of Holdover.ramp
# Transmission whose end the estimate cases are measured from.
HIST_END_UNIX = 1760900000.0
HIST_E0_PPM = -3.5228
HIST_DURATION_S = 320.0
EST_DELAYS_S = [1.0, 1200.0, 86400.0]


def _num(v: float) -> str:
    return "%.17g" % float(v)


def _array(name: str, fields: list[str], rows: list[list[float]], comment: str) -> str:
    out = [f"/* {comment} */", "static const struct {"]
    out += [f"    double {f};" for f in fields]
    out.append(f"}} {name}[] = {{")
    out += ["    {" + ", ".join(_num(v) for v in r) + "}," for r in rows]
    out += ["};", f"#define {name}_N {len(rows)}", ""]
    return "\n".join(out)


def xo_cases() -> list[list[float]]:
    rows = []
    for osc in OSC_PPM:
        xo_ppm, residual = split_osc_error(osc)
        xo_hz = int(round(40e6 * (1.0 + osc * 1e-6)))
        rows.append([osc, xo_hz, xo_ppm, residual])
    return rows


def ramp_cases(ho: Holdover) -> list[list[float]]:
    rows = []
    for e0 in E0_PPM:
        for dur in DURATIONS:
            rate, accel = ho.ramp(e0, dur)
            end_ppb = float(ho.error_ppb(e0, [dur])[0])
            rows.append([e0, dur, rate, accel, end_ppb])
    return rows


def est_cases() -> list[list[float]]:
    rows = []
    cold = Holdover()
    e0, sigma = cold.estimate_start(HIST_END_UNIX)
    rows.append([0.0, 0.0, HIST_END_UNIX, e0, sigma])       # no history: last_end_unix = 0
    ho = Holdover()
    ho.mark_end(HIST_END_UNIX, HIST_E0_PPM, HIST_DURATION_S)
    for delay in EST_DELAYS_S:
        now = HIST_END_UNIX + delay
        e0, sigma = ho.estimate_start(now)
        rows.append([ho.last_end_unix, ho.last_end_ppm, now, e0, sigma])
    return rows


def dec_cases(ho: Holdover) -> list[list[float]]:
    rows = []
    for e0 in E0_PPM:
        for dur in DURATIONS:
            if dur <= 0:
                continue
            rate, accel = ho.ramp(e0, dur)
            d = decision(ho, e0, rate, accel, dur, LAT)
            rows.append([e0, dur, d["valid_s"], d["max_err_ppb"], d["xo_hz"], d["residual_ppb"],
                         d["lo_shift_hz"], d["end_ppb"]])
    return rows


def main(out_path) -> None:
    ho = Holdover()
    parts = ["/* generated, do not edit */",
             "/* source: scripts/gen_gpsdo_vectors.py (gps-sim); Holdover() defaults */",
             "/* regenerate (in gps-sim): PYTHONPATH=. python scripts/gen_gpsdo_vectors.py <tezuka>/app/gpsdod/test/vectors.h */",
             "#ifndef GPSDOD_VECTORS_H",
             "#define GPSDOD_VECTORS_H", "",
             _array("XO_CASES", ["osc_ppm", "xo_hz", "xo_ppm", "residual"], xo_cases(),
                    "backend.rf.xo.split_osc_error"),
             _array("RAMP_CASES", ["e0_ppm", "duration_s", "rate", "accel", "end_ppb"], ramp_cases(ho),
                    "Holdover.ramp / error_ppb; duration 0 is the Taylor-at-start branch"),
             _array("EST_CASES", ["last_end_unix", "last_end_ppm", "now_unix", "e0_ppm", "sigma_ppb"], est_cases(),
                    "Holdover.estimate_start; last_end_unix = 0 means no history"),
             _array("DEC_CASES", ["e0_ppm", "duration_s", "valid_s", "max_err_ppb", "xo_hz", "residual_ppb",
                                  "lo_shift_hz", "end_ppb"], dec_cases(ho),
                    "scripts.gpsdo_cycle.decision (values rounded as the reference rounds them)"),
             "#endif", ""]
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(parts), encoding="utf8", newline="\n")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "vectors.h")
