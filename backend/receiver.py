from __future__ import annotations

import numpy as np

from backend import config, geometry, inspector


def solve_position(pseudoranges, sat_positions, x0=None) -> dict:
    prns = sorted(pseudoranges)
    S = np.array([sat_positions[p] for p in prns], float)
    pr = np.array([pseudoranges[p] for p in prns], float)
    X = np.zeros(4) if x0 is None else np.array(x0, float)
    it = 0
    for it in range(1, 15):
        rng = np.linalg.norm(S - X[:3], axis=1)
        pred = rng + config.C * X[3]
        dz = pr - pred
        H = np.column_stack([(X[:3] - S) / rng[:, None], config.C * np.ones(len(prns))])
        dX, *_ = np.linalg.lstsq(H, dz, rcond=None)
        X += dX
        if np.linalg.norm(dX[:3]) < 1e-4:
            break
    rng = np.linalg.norm(S - X[:3], axis=1)
    resid = pr - (rng + config.C * X[3])
    return {
        "ecef": X[:3].tolist(),
        "clock_bias_s": float(X[3]),
        "iterations": it,
        "residual_rms_m": float(np.sqrt(np.mean(resid ** 2))),
    }


def _ecef_to_llh(x, y, z):
    a, e2 = 6378137.0, 6.69437999014e-3
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1 - e2))
    for _ in range(6):
        n = a / np.sqrt(1 - e2 * np.sin(lat) ** 2)
        h = p / np.cos(lat) - n
        lat = np.arctan2(z, p * (1 - e2 * n / (n + h)))
    return np.degrees(lat), np.degrees(lon), float(h)


def fix_from_iq(iq_path, sample_format, sample_rate, eph_by_prn,
                approx_time_gps, marker_llh=None) -> dict:
    iq = inspector.read_iq(iq_path, sample_format, max_samples=int(sample_rate * 0.020))
    approx_rx = np.array(geometry.llh_to_ecef(*marker_llh)) if marker_llh else np.zeros(3)

    acq = {}
    for prn, eph in eph_by_prn.items():
        r = inspector.acquire(iq, sample_rate, prn)
        if r["metric_db"] > 9:
            acq[prn] = r
    if len(acq) < 4:
        return {"error": f"only {len(acq)} PRNs acquired", "prns_used": sorted(acq)}

    sat_pos, predicted = {}, {}
    for prn in acq:
        pos, _, tof, clk = geometry.solve_transmit_time(eph_by_prn[prn], approx_rx, approx_time_gps)
        sat_pos[prn] = pos
        predicted[prn] = np.linalg.norm(pos - approx_rx) - config.C * clk

    ref = max(acq, key=lambda p: predicted[p] * -1)  # highest elevation ~ shortest range
    code_m = {p: (acq[p]["code_phase_chips"] / config.CA_CHIP_HZ) * config.C for p in acq}
    pr = {}
    for prn in acq:
        n_ms = round((predicted[prn] - predicted[ref] - (code_m[prn] - code_m[ref]))
                     / (config.C * 1e-3))
        pr[prn] = predicted[ref] + (code_m[prn] - code_m[ref]) + n_ms * config.C * 1e-3

    sol = solve_position(pr, sat_pos, x0=[*approx_rx, 0.0])
    lat, lon, h = _ecef_to_llh(*sol["ecef"])
    entries = []
    for prn in acq:
        los = (sat_pos[prn] - np.array(sol["ecef"]))
        entries.append({"_los": (los / np.linalg.norm(los)).tolist()})
    out = {
        "ecef": sol["ecef"], "llh": [lat, lon, h],
        "clock_bias_s": sol["clock_bias_s"], "prns_used": sorted(acq),
        "pdop": geometry.dop(entries, sol["ecef"])["pdop"],
        "residual_rms_m": sol["residual_rms_m"],
    }
    if marker_llh:
        truth = np.array(geometry.llh_to_ecef(*marker_llh))
        out["error_m"] = float(np.linalg.norm(np.array(sol["ecef"]) - truth))
    return out
