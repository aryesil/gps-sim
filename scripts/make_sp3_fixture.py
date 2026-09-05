#!/usr/bin/env python3
"""Generate tests/fixtures/igs_sample.sp3 -- a synthetic but realistic SP3-d.

The orbit samples are the broadcast ephemeris in tests/fixtures/brdc_sample.rnx
propagated with backend.geometry (the project's own Kepler model) at a 15 min
cadence, plus a small deterministic position bias and a linear clock offset per
PRN so a broadcast-vs-precise comparison shows non-zero, bounded deltas.

Because the underlying curve is a smooth Kepler orbit, interpolation accuracy
can be checked against geometry.sat_state at an off-grid epoch
(tests/test_precise.py). The per-PRN bias/clock offsets are recorded in
igs_sample_truth.json.

Run from the repo root:  python scripts/make_sp3_fixture.py
"""
from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from backend import ephemeris, geometry  # noqa: E402

GPS_EPOCH = dt.datetime(1980, 1, 6)
INTERVAL_S = 900.0
HALF_SPAN_EPOCHS = 20            # +/- 5 h around the broadcast toe
FIX = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "brdc_sample.rnx"


def _bias_for(prn: int) -> tuple[float, float, float, float, float]:
    """Deterministic (dx, dy, dz metres, clk_c0 s, clk_c1 s/s) per PRN."""
    r = (prn * 2654435761) & 0xFFFFFFFF
    dx = ((r % 97) - 48) * 0.03           # +/- ~1.4 m
    dy = (((r >> 7) % 97) - 48) * 0.03
    dz = (((r >> 14) % 97) - 48) * 0.03
    c0 = (((r >> 3) % 200) - 100) * 1e-7   # +/- 10 us
    c1 = (((r >> 11) % 200) - 100) * 1e-13
    return dx, dy, dz, c0, c1


def main() -> None:
    root = pathlib.Path(__file__).resolve().parent.parent
    out = root / "tests" / "fixtures" / "igs_sample.sp3"
    eph = ephemeris.parse_rinex(FIX)
    prns = sorted(eph)

    # Fixture broadcast epoch: toe of the first PRN (all near 475200 SOW,
    # 2026-08-28). Recover week from GPSWeek and build the wall-clock start.
    toe = eph[prns[0]]["toe"]
    week = int(eph[prns[0]]["gps_week"])
    center = GPS_EPOCH + dt.timedelta(weeks=week, seconds=toe)  # GPS timescale
    start_dt = center - dt.timedelta(seconds=HALF_SPAN_EPOCHS * INTERVAL_S)
    n_epochs = 2 * HALF_SPAN_EPOCHS + 1
    start_sow = toe - HALF_SPAN_EPOCHS * INTERVAL_S

    L: list[str] = []
    L.append(f"#dP{start_dt:%Y %m %d %H %M %S}.00000000 {n_epochs:6d} ORBIT IGb14 HLM  SYNTH")
    L.append(f"## {week:4d} {start_sow:15.8f} {INTERVAL_S:14.8f} {58000:5d} 0.0000000000000")
    sat_ids = "".join(f"G{p:02d}" for p in prns)
    pad = "G00" * max(0, 17 - len(prns))
    L.append(f"+  {len(prns):3d}   " + sat_ids + pad)
    for _ in range(4):
        L.append("+          " + "  0" * 17)
    for _ in range(5):
        L.append("++         " + "  0" * 17)
    L.append("%c M  cc GPS ccc cccc cccc cccc cccc ccccc ccccc ccccc ccccc")
    L.append("%c cc cc ccc ccc cccc cccc cccc cccc ccccc ccccc ccccc ccccc")
    L.append("%f  1.2500000  1.025000000  0.00000000000  0.000000000000000")
    L.append("%f  0.0000000  0.000000000  0.00000000000  0.000000000000000")
    L.append("%i    0    0    0    0      0      0      0      0         0")
    L.append("%i    0    0    0    0      0      0      0      0         0")
    L.append("/* SYNTHETIC SP3 FIXTURE -- broadcast ephemeris propagated with")
    L.append("/* backend.geometry, plus a small per-PRN bias. Not a real product.")

    biases = {p: _bias_for(p) for p in prns}
    for k in range(n_epochs):
        sow = start_sow + k * INTERVAL_S
        ep = start_dt + dt.timedelta(seconds=k * INTERVAL_S)
        L.append(f"*  {ep:%Y %m %d %H %M}{ep.second:3d}.00000000")
        for p in prns:
            pos, _vel, clk = geometry.sat_state(eph[p], sow)
            dx, dy, dz, c0, c1 = biases[p]
            x = (pos[0] + dx) / 1000.0
            y = (pos[1] + dy) / 1000.0
            z = (pos[2] + dz) / 1000.0
            clk_us = (clk + c0 + c1 * (sow - toe)) * 1e6
            L.append(f"PG{p:02d}{x:14.6f}{y:14.6f}{z:14.6f}{clk_us:14.6f}")
    L.append("EOF")
    out.write_text("\n".join(L) + "\n")

    truth = {
        "gps_week": week, "toe_sow": toe, "start_sow": start_sow,
        "interval_s": INTERVAL_S, "n_epochs": n_epochs, "prns": prns,
        "bias_by_prn": {str(p): {"dx_m": biases[p][0], "dy_m": biases[p][1],
                                 "dz_m": biases[p][2], "clk_c0_s": biases[p][3],
                                 "clk_c1_sps": biases[p][4]} for p in prns},
    }
    out.with_name("igs_sample_truth.json").write_text(json.dumps(truth, indent=2) + "\n")
    print(f"wrote {out} ({out.stat().st_size} bytes), {n_epochs} epochs, PRNs {prns}")


if __name__ == "__main__":
    main()
