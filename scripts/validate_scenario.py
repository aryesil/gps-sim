#!/usr/bin/env python3
"""End-to-end scenario validation.

Chains the checks that together say whether a scenario is internally
consistent and physically plausible:

  1. ephemeris        -- parse the broadcast RINEX, list PRNs / health
  2. geometry         -- production propagation vs the independent
                         IS-GPS-200 reference (backend.reference)
  3. generation       -- run gps-sdr-sim (skipped with --no-generate or
                         when no binary is available)
  4. iq_integrity     -- structure + statistics of the .bin
  5. acquisition      -- re-acquire the planted PRNs from the IQ
  6. receiver         -- weighted least-squares fix vs the known truth
  7. error_budget     -- nominal per-PRN UERE roll-up

Each stage yields {status: pass|warn|fail|skip, ...}. Exit code is 0
when no stage failed. ``--json`` prints the machine-readable report;
the default is a human summary.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import (config, ephemeris, error_budget, geometry, inspector,  # noqa: E402
                     iq_integrity, receiver, reference, scenario, truth)

_STAGES = ("ephemeris", "geometry", "generation", "iq_integrity",
           "acquisition", "receiver", "error_budget")


def _stage(status, **kw):
    return {"status": status, **kw}


def _check_geometry(eph_by_prn, t: truth.ScenarioTruth, offset_s=0.0):
    rx = np.array(t.rx_ecef)
    sow = t.sow_at(offset_s)
    worst_pos = 0.0
    worst_dopp = 0.0
    n = 0
    for prn, e in eph_by_prn.items():
        try:
            a = geometry.solve_transmit_time(e, rx, sow)
            b = reference.solve_transmit_time(e, rx, sow)
        except Exception:
            continue
        worst_pos = max(worst_pos, float(np.linalg.norm(a[0] - b["sat_ecef"])))
        o = geometry.observables(e, rx, sow)
        worst_dopp = max(worst_dopp, abs(o["carrier_doppler_hz"] - b["carrier_doppler_hz"]))
        n += 1
    status = "pass" if (worst_pos < 1e-2 and worst_dopp < 1.0) else "fail"
    return _stage(status, sats_checked=n, worst_position_diff_m=worst_pos,
                  worst_doppler_diff_hz=worst_dopp,
                  note="production geometry vs independent IS-GPS-200 reference")


def run_validation(req: scenario.ScenarioRequest, *, binary: str | None = None,
                   do_generate: bool = True, mask_deg: float = 5.0) -> dict:
    from backend import generator

    t = truth.from_request(req)
    report: dict = {
        "scenario": t.as_dict(),
        "created_utc": dt.datetime.utcnow().isoformat(),
        "stages": {s: _stage("skip") for s in _STAGES},
    }
    st = report["stages"]

    # 1. ephemeris
    try:
        eph_by_prn = ephemeris.parse_rinex(req.rinex_path)
        st["ephemeris"] = _stage("pass" if eph_by_prn else "fail",
                                 prns=sorted(eph_by_prn))
    except Exception as exc:                       # noqa: BLE001
        st["ephemeris"] = _stage("fail", error=str(exc))
        report["ok"] = False
        return report

    # 2. geometry
    st["geometry"] = _check_geometry(eph_by_prn, t)

    # 3. generation
    outdir = None
    if do_generate:
        b = binary or config.GPS_SDR_SIM_BIN
        try:
            outdir = generator.run(req, binary=b)
            st["generation"] = _stage("pass", outdir=str(outdir))
        except Exception as exc:                   # noqa: BLE001
            st["generation"] = _stage("fail", error=str(exc))

    # 4-6 need the .bin
    if outdir is not None and (outdir / "gpssim.bin").exists():
        meta = json.loads((outdir / "meta.json").read_text())
        ig = meta.get("iq_integrity")
        if ig is not None:
            st["iq_integrity"] = _stage("pass" if ig.get("ok") else "warn",
                                        problems=ig.get("problems", []))
        approx_gps = t.start_sow
        try:
            fix = receiver.fix_from_iq(
                outdir / "gpssim.bin", req.sample_format, req.sample_rate,
                eph_by_prn, approx_gps, marker_llh=(req.lat, req.lon, req.alt))
        except Exception as exc:                   # noqa: BLE001
            fix = {"error": str(exc)}
        if "error" in fix:
            st["acquisition"] = _stage("fail", error=fix["error"])
        else:
            st["acquisition"] = _stage(
                "pass" if len(fix["prns_used"]) >= 4 else "fail",
                prns_used=fix["prns_used"])
            err = fix.get("error_m")
            rstat = "pass" if (err is not None and err < 100.0) else "warn"
            st["receiver"] = _stage(rstat, error_m=err,
                                    wls=fix.get("wls"),
                                    residual_rms_m=fix.get("residual_rms_m"))
            if fix.get("error_budget"):
                st["error_budget"] = _stage("pass", **fix["error_budget"])

    if not do_generate:
        # geometry-only run: still summarise a nominal budget from truth
        obs = t.observables(eph_by_prn, mask_deg=mask_deg)
        st["error_budget"] = _stage("pass", **error_budget.summarize(
            [error_budget.budget_for_prn(o["prn"], elevation_deg=o["el_deg"])
             for o in obs]))

    report["ok"] = all(v["status"] != "fail" for v in st.values())
    return report


def _human(report: dict) -> str:
    lines = ["scenario validation", "=" * 40]
    sc = report["scenario"]
    lines.append(f"  site   : {sc['lat_deg']:.5f}, {sc['lon_deg']:.5f}, {sc['alt_m']:.1f} m")
    lines.append(f"  start  : {sc['start_utc']}  (GPS week {sc['gps_week']}, "
                 f"sow {sc['start_sow']:.1f})")
    lines.append("")
    for name, s in report["stages"].items():
        mark = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "skip"}[s["status"]]
        extra = {k: v for k, v in s.items() if k != "status"}
        detail = ""
        if "worst_position_diff_m" in extra:
            detail = f"pos<{extra['worst_position_diff_m']:.2e} m, dopp<{extra['worst_doppler_diff_hz']:.2e} Hz"
        elif "error_m" in extra and extra["error_m"] is not None:
            detail = f"fix error {extra['error_m']:.1f} m"
        elif "prns" in extra:
            detail = f"{len(extra['prns'])} PRNs"
        elif "uere_rms_m" in extra and extra["uere_rms_m"]:
            detail = f"UERE rms {extra['uere_rms_m']:.2f} m"
        elif "error" in extra:
            detail = str(extra["error"])[:80]
        lines.append(f"  {name:<13} {mark:<5} {detail}")
    lines.append("")
    lines.append(f"  OVERALL: {'PASS' if report['ok'] else 'FAIL'}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rinex", help="broadcast RINEX nav file")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--alt", type=float, default=100.0)
    ap.add_argument("--start", required=True, help="UTC start, ISO 8601")
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--sample-rate", type=float, default=config.DEFAULT_SAMPLE_RATE)
    ap.add_argument("--no-generate", action="store_true",
                    help="skip gps-sdr-sim; run the geometry / budget stages only")
    ap.add_argument("--json", action="store_true", help="emit the JSON report")
    args = ap.parse_args(argv)

    req = scenario.ScenarioRequest(
        rinex_path=args.rinex, lat=args.lat, lon=args.lon, alt=args.alt,
        start=dt.datetime.fromisoformat(args.start), duration_s=args.duration,
        sample_rate=args.sample_rate)
    report = run_validation(req, do_generate=not args.no_generate)
    print(json.dumps(report, indent=2) if args.json else _human(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
