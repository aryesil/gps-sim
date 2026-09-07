from __future__ import annotations

import numpy as np

from backend import config, geometry, inspector
from backend.models import error_budget, wls


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


_CM_LEN = 10230
_CM_CHIP_HZ = 0.5115e6


def _fix_from_iq_l2(iq_path, sample_format, sample_rate, eph_by_prn,
                    approx_time_gps, marker_llh) -> dict:
    """GPS L2C closed-loop fix: acquire CM, decode CNAV 10/11/30, solve."""
    from backend.analysis import band_acquire, cnav_decode
    from backend.synth import _lib
    from backend.synth import signals as _sig

    l2c = _sig.SIGNALS["GPS_L2C"]
    fl1, fl2 = config.L1_HZ, config.L2_HZ
    m_per_chip = config.C / _CM_CHIP_HZ

    # CNAV messages 10 + 11 arrive once per 12 s each; 50 s of data gives a
    # comfortable margin for acquisition slack plus a full 10/11/30 set.
    iq = inspector.read_iq(iq_path, sample_format,
                           max_samples=int(sample_rate * 50.0))
    approx_rx = (np.array(geometry.llh_to_ecef(*marker_llh))
                 if marker_llh else np.zeros(3))

    scan = eph_by_prn if eph_by_prn else {p: None for p in range(1, 33)}
    acq = {}
    for prn in scan:
        r = band_acquire.acquire_l2c(iq, sample_rate, prn)
        if r["metric_db"] > 11.0:
            acq[prn] = r

    decoded, nav_decode = {}, {}
    for prn, r in list(acq.items()):
        try:
            sym = cnav_decode.demod_symbols(iq, sample_rate, prn,
                                            dopp_hz=r["doppler_hz"],
                                            code_phase_chips=r["code_phase_chips"])
            msgs = cnav_decode.decode_messages(sym.tolist())
            # Only trust CRC-valid messages whose PRN field is this SV: a
            # cross-correlation lock onto a sibling at a near-equal Doppler
            # decodes that sibling's CNAV, which would otherwise be assigned
            # the wrong geometry.
            own = [msg for msg in msgs if msg["crc_ok"] and msg["prn"] == prn]
            if not ({10, 11} <= {msg["type"] for msg in own}):
                nav_decode[prn] = "no own-PRN 10/11"
                continue
            rec = cnav_decode.reconstruct_ephemeris(own)
            rec["prn"] = prn
            decoded[prn] = rec
            nav_decode[prn] = "ok"
        except (ValueError, IndexError):
            nav_decode[prn] = "decode fail"

    if len(decoded) < 4:
        return {"error": f"only {len(decoded)} PRNs decoded on L2C",
                "prns_used": sorted(decoded), "nav_decode": nav_decode}

    sat_pos, pr, usable = {}, {}, []
    for prn in sorted(decoded):
        rec = decoded[prn]
        tgd = rec.get("tgd", 0.0)
        svb = -tgd * (fl1 / fl2) ** 2 + rec.get("isc_l2c", 0.0)
        o = geometry.observables(rec, approx_rx, approx_time_gps,
                                 signal=l2c, sv_clock_bias_s=svb)
        if o["el_deg"] < 5.0:
            nav_decode[prn] = "low el"
            continue
        pos, _, _, clk = geometry.solve_transmit_time(rec, approx_rx,
                                                      approx_time_gps)
        # band_acquire and observables both report the code phase as the
        # gps-sdr-sim correlation lag (rho/c in chips), so they subtract
        # directly; wrap the difference into +/- half a code period. The
        # coarse acquisition lag is only sample-resolved (~0.2 chip), so
        # refine it with a parabolic peak fit at the acquired Doppler.
        cm, _cl = _lib.code_l2c(int(prn))
        cp = band_acquire.fine_code_phase(
            iq, sample_rate, cm.astype(float), chip_hz=_CM_CHIP_HZ,
            code_len=_CM_LEN, dopp_hz=acq[prn]["doppler_hz"])
        err_c = ((cp - o["code_phase_chips"] + _CM_LEN / 2) % _CM_LEN
                 ) - _CM_LEN / 2
        if abs(err_c) > 0.45 * _CM_LEN:
            nav_decode[prn] = "unaligned"
            continue
        sat_pos[prn] = pos
        pr[prn] = o["pseudorange_m"] + err_c * m_per_chip + config.C * (clk + svb)
        usable.append(prn)

    if len(usable) < 4:
        return {"error": f"only {len(usable)} PRNs usable on L2C",
                "prns_used": sorted(usable), "nav_decode": nav_decode}

    sol = solve_position(pr, sat_pos, x0=[*approx_rx, 0.0])
    lat, lon, h = _ecef_to_llh(*sol["ecef"])
    entries = []
    for prn in usable:
        los = sat_pos[prn] - np.array(sol["ecef"])
        entries.append({"_los": (los / np.linalg.norm(los)).tolist()})
    out = {
        "ecef": sol["ecef"], "llh": [lat, lon, h],
        "clock_bias_s": sol["clock_bias_s"], "prns_used": sorted(usable),
        "pdop": geometry.dop(entries, sol["ecef"])["pdop"],
        "residual_rms_m": sol["residual_rms_m"], "nav_decode": nav_decode,
    }
    if marker_llh:
        truth = np.array(geometry.llh_to_ecef(*marker_llh))
        out["error_m"] = float(np.linalg.norm(np.array(sol["ecef"]) - truth))
    return out


def fix_from_iq(iq_path, sample_format, sample_rate, eph_by_prn,
                approx_time_gps, marker_llh=None, decode_nav=False,
                *, band: str = "L1") -> dict:
    """``band`` selects the signal family. ``"L1"`` is the GPS L1 C/A path
    (all params as before). ``"L2"`` runs the GPS L2C path: CM acquisition
    via :mod:`backend.analysis.band_acquire`, CNAV ephemeris decode via
    :mod:`backend.analysis.cnav_decode`, then the same WLS position solve.
    """
    if band == "L2":
        return _fix_from_iq_l2(iq_path, sample_format, sample_rate,
                               eph_by_prn, approx_time_gps, marker_llh)
    if band != "L1":
        raise ValueError(f"unsupported band {band!r}")
    # A LNAV decode needs at least one full set of subframes 1-3 (18 s of
    # 50 bps data) plus acquisition slack; a plain code-phase fix needs
    # only a short slice.
    span_s = 20.0 if decode_nav else 0.020
    iq = inspector.read_iq(iq_path, sample_format,
                           max_samples=int(sample_rate * span_s))
    approx_rx = np.array(geometry.llh_to_ecef(*marker_llh)) if marker_llh else np.zeros(3)

    scan = eph_by_prn if eph_by_prn else {p: None for p in range(1, 33)}
    acq = {}
    for prn in scan:
        r = inspector.acquire(iq, sample_rate, prn)
        if r["metric_db"] > 9:
            acq[prn] = r

    nav_decode: dict = {}
    if decode_nav:
        from backend.analysis import lnav_decode as ld

        decoded = {}
        for prn, r in list(acq.items()):
            bits = ld.demod_nav_bits(iq, sample_rate, prn,
                                     r["code_phase_chips"], r["doppler_hz"],
                                     n_bits=1200)
            sfs = {h["subframe_id"]: h["words"] for h in ld.find_frame(bits)}
            if {1, 2, 3} <= set(sfs):
                try:
                    decoded[prn] = ld.decode_ephemeris(sfs)
                    nav_decode[prn] = "ok"
                except ValueError:
                    nav_decode[prn] = "parity"
            else:
                nav_decode[prn] = "no frame"
        eph_by_prn = decoded
        acq = {p: acq[p] for p in decoded}

    if len(acq) < 4:
        out = {"error": f"only {len(acq)} PRNs acquired", "prns_used": sorted(acq)}
        if decode_nav:
            out["nav_decode"] = nav_decode
        return out

    if decode_nav:
        # Anchor each pseudorange to the value predicted from the decoded
        # ephemeris at the a-priori position/time (this carries the integer
        # millisecond count), then apply the measured sub-chip residual.
        # Robust for an a-priori within ~150 km / a known approximate time.
        sats = geometry.constellation(eph_by_prn, approx_rx, approx_time_gps,
                                      mask_deg=-90.0)
        ent = {e["prn"]: e for e in sats}
        sat_pos, pr, usable = {}, {}, []
        m_per_chip = config.C / config.CA_CHIP_HZ
        for prn in list(acq):
            e = ent.get(prn)
            if e is None:
                nav_decode[prn] = "no geometry"
                continue
            err_c = ((acq[prn]["code_phase_chips"] - (e["code_phase_chips"] % 1023)
                      + 511.5) % 1023) - 511.5
            if abs(err_c) > 450:
                nav_decode[prn] = "unaligned"
                continue
            pos, _, _, clk = geometry.solve_transmit_time(
                eph_by_prn[prn], approx_rx, approx_time_gps)
            sat_pos[prn] = pos
            # observables() models the raw measurement as geo - c*dt_sv;
            # add the decoded satellite-clock term back to get a
            # clock-corrected pseudorange for the position solve.
            pr[prn] = (e["pseudorange_m"] + err_c * m_per_chip
                       + config.C * clk)
            usable.append(prn)
        acq = {p: acq[p] for p in usable}
        if len(acq) < 4:
            return {"error": f"only {len(acq)} PRNs usable after nav decode",
                    "prns_used": sorted(acq), "nav_decode": nav_decode}
    else:
        sat_pos, predicted = {}, {}
        for prn in acq:
            pos, _, tof, clk = geometry.solve_transmit_time(
                eph_by_prn[prn], approx_rx, approx_time_gps)
            sat_pos[prn] = pos
            predicted[prn] = np.linalg.norm(pos - approx_rx) - config.C * clk

        ref = max(acq, key=lambda p: predicted[p] * -1)  # shortest range ~ highest el
        code_m = {p: (acq[p]["code_phase_chips"] / config.CA_CHIP_HZ) * config.C
                  for p in acq}
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

    # elevation-weighted least-squares solution + DOP block + a nominal
    # per-PRN error budget, alongside the legacy unweighted fix above.
    try:
        rx_sol = np.array(sol["ecef"])
        el_by_prn, weights = {}, {}
        for prn in acq:
            los = sat_pos[prn] - rx_sol
            up = rx_sol / np.linalg.norm(rx_sol)
            el_by_prn[prn] = float(np.degrees(np.arcsin(
                np.clip((los / np.linalg.norm(los)) @ up, -1, 1))))
            weights[prn] = wls.elevation_weight(el_by_prn[prn])
        wsol = wls.solve(pr, {p: sat_pos[p] for p in acq}, weights=weights,
                         x0=[*approx_rx, 0.0])
        wlat, wlon, wh = _ecef_to_llh(*wsol["ecef"])
        out["wls"] = {
            "llh": [wlat, wlon, wh], "ecef": wsol["ecef"],
            "clock_bias_s": wsol["clock_bias_s"],
            "dop": wsol["dop"],
            "residual_rms_m": wsol["residual_rms_m"],
            "weighted_residual_rms_m": wsol["weighted_residual_rms_m"],
            "sigma_horizontal_m": wsol["sigma_horizontal_m"],
            "sigma_vertical_m": wsol["sigma_vertical_m"],
        }
        out["error_budget"] = error_budget.summarize([
            error_budget.budget_for_prn(p, elevation_deg=el_by_prn[p])
            for p in sorted(acq)])
    except (ValueError, np.linalg.LinAlgError):
        out["wls"] = None
    if marker_llh:
        truth = np.array(geometry.llh_to_ecef(*marker_llh))
        out["error_m"] = float(np.linalg.norm(np.array(sol["ecef"]) - truth))
    if decode_nav:
        out["nav_decode"] = nav_decode
    return out
