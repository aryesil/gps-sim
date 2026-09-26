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
_L5_LEN = 10230
_L5_CHIP_HZ = 10.23e6
# Acquisition threshold for the L5-band blind scan (GPS/QZSS I5, Galileo
# E5a-I, BeiDou B2a, NavIC L5-SPS). One 1 ms period is fs*1ms code-phase
# bins x 121 Doppler bins, so the largest noise-only cell already sits
# ~12-15 dB above the median (measured on absent PRNs at 2.1 and 25 Msps).
# A 9 dB threshold let every scanned PRN through and each false hit then
# paid for a full 26-44 s nav demod, which is what kept the L5/E5a/B2a
# closed-loop tests from finishing. Real satellites measure 21-35 dB, so
# 18 dB separates the two.
_L5_ACQ_DB = 18.0
# Seconds of L5-band IQ _fix_from_iq_l5 reads (see the comment there).
_L5_READ_S = 32.0
_NAVIC_LEN = 1023
_NAVIC_CHIP_HZ = 1.023e6


def _fix_from_iq_l2(iq_path, sample_format, sample_rate, eph_by_prn,
                    approx_time_gps, marker_llh) -> dict:
    """GPS L2C closed-loop fix: acquire CM, decode CNAV 10/11/30, solve."""
    from backend.analysis import band_acquire, cnav_decode
    from backend.synth import signals as _sig

    l2c = _sig.SIGNALS["GPS_L2C"]
    m_per_chip = config.C / _CM_CHIP_HZ

    # L2C CNAV messages arrive one per 12 s (10, 11, 30, 33 cycle); 50 s of
    # data gives a comfortable margin for acquisition slack plus a full
    # 10/11/30 set.
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
            if not ({10, 11, 30} <= {msg["type"] for msg in own}):
                nav_decode[prn] = "no own-PRN 10/11/30"
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
        svb = geometry.group_delay_bias_s("G", "L2", rec)
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
        cp = band_acquire.fine_code_phase(
            iq, sample_rate, band_acquire.l2c_cm_replica(prn),
            chip_hz=band_acquire.L2C_TDM_CHIP_HZ,
            code_len=band_acquire.L2C_TDM_LEN,
            dopp_hz=acq[prn]["doppler_hz"]) / 2.0      # TDM slots -> CM chips
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


def _fix_from_iq_l5(iq_path, sample_format, sample_rate, eph_by_prn,
                    approx_time_gps, marker_llh) -> dict:
    """L5-band closed-loop fix: acquire GPS/QZSS I5 (NH10-blind
    non-coherent), Galileo E5a-I (CS20-blind), NavIC L5-SPS and BeiDou
    B2a-data (5-chip-secondary-blind), decode CNAV 10/11/30 off I5, F/NAV
    word types 1/2/3(+4) off E5a-I, NavIC subframes 1/2 off the L5-SPS
    component, and B-CNAV2 10/11(+30) off B2a-data, solve jointly.
    GPS/QZSS L5, Galileo E5a-I, NavIC L5-SPS and BeiDou B2a-data all share
    this RF band (all four centre on 1176.45 MHz), so a capture can carry
    any subset of the four; every acquired/decoded satellite is keyed by
    ``(sys, prn)`` throughout (all four number PRNs from 1, so a bare PRN
    key would collide across families -- the same reasoning as the
    engine's own ``nav_streams`` keying).

    Structurally identical to :func:`_fix_from_iq_l2`; the differences are
    the per-system code (10.23 Mcps I5/E5a-I/B2a-data vs. NavIC's
    1.023 Mcps L5-SPS), the L5 band centre for the geometry, and the
    per-system group delay: GPS/QZSS uses ``-Tgd*(fL1/fL5)^2 + ISC_L5I5``,
    Galileo's single-frequency BGD(E1,E5a) and NavIC's single-frequency
    TGD are both applied directly (no frequency-squared scaling or ISC
    term -- NavIC L5-SPS carries no second civil frequency and no ISC
    field in subframes 1/2, matching Galileo's own single-frequency
    convention here, not an omission), and BeiDou B2a uses
    ``-TGD_B2ap + ISC_B2ad`` (same -Tgd+ISC shape as GPS).
    """
    from backend.analysis import nav_encoders
    from backend.analysis import (band_acquire, bcnav2_decode, cnav_decode,
                                  fnav_decode, navic_decode)
    from backend.synth import _lib
    from backend.synth import signals as _sig

    l5, e5a = _sig.SIGNALS["GPS_L5I"], _sig.SIGNALS["GAL_E5AI"]
    navic = _sig.SIGNALS["IRNSS_L5"]
    b2a = _sig.SIGNALS["BDS_B2AD"]

    # A full L5 CNAV 10/11/30/33 cycle spans 24 s (6 s messages); F/NAV
    # needs pages 1-3, i.e. 30 s from a page-1 boundary. The signal reaches
    # the receiver ~70-80 ms after it leaves the satellite, so a capture
    # that opens on that boundary holds page 3's last symbol just past
    # 30 s: a 30 s read cut it off and every E5a satellite reported "no
    # own-PRN 1/2/3". L5 runs at ~25 Msps, so keep the cap close to that
    # (a 50 s cap would be ~1.3 G samples); read_iq clamps to the file
    # length anyway.
    iq = inspector.read_iq(iq_path, sample_format,
                           max_samples=int(sample_rate * _L5_READ_S))
    approx_rx = (np.array(geometry.llh_to_ecef(*marker_llh))
                 if marker_llh else np.zeros(3))

    # eph_by_prn (when supplied) is PRN-only keyed with no per-system
    # scoping; the blind scan below (the tested path, eph_by_prn={}) is
    # unambiguous since it enumerates each family's own PRN range.
    gj_scan = eph_by_prn if eph_by_prn else {p: None for p in range(1, 33)}
    gal_scan = {} if eph_by_prn else {p: None for p in range(1, 37)}
    navic_scan = {} if eph_by_prn else {p: None for p in range(1, 15)}
    bds_scan = {} if eph_by_prn else {p: None for p in range(1, 64)}

    # GPS/QZSS I5 and Galileo E5a-I both run at 10.23 Mcps; a capture
    # whose fs cannot represent that (fs below 2x the chip rate -- e.g.
    # a NavIC-only scenario deliberately captured at NavIC's own much
    # lower ~2.1 Msps floor) aliases every PRN's correlator into a
    # meaningless peak above the acquisition threshold instead of
    # correctly reporting "no signal", so each of the 68 false hits then
    # pays for a full (slow) CNAV/F-NAV demod attempt. Skip both
    # families outright when fs cannot resolve their chip rate; same
    # guard for NavIC's own (much lower) chip rate for symmetry.
    if sample_rate < 2.0 * _L5_CHIP_HZ:
        gj_scan, gal_scan, bds_scan = {}, {}, {}
    if sample_rate < 2.0 * _NAVIC_CHIP_HZ:
        navic_scan = {}

    acq: dict[tuple[str, int], dict] = {}
    for prn in gj_scan:
        i5, _q5 = _lib.code_l5(int(prn))
        # One coherent I5 period (1 ms) at fs >= ~25 Msps: the 10.23 Mcps
        # main lobe is wide, so a single period already clears ~13 dB, and
        # non-coherent stacking of more periods loses to squaring at this
        # SNR. NH10 is real +/-1, so it does not matter for |.|.
        r = band_acquire.acquire(iq, sample_rate, i5.astype(float),
                                 chip_hz=_L5_CHIP_HZ, code_len=_L5_LEN,
                                 dopp_step=100.0, nperiods=1)
        if r["metric_db"] > _L5_ACQ_DB:
            acq[("G", prn)] = r
    for prn in gal_scan:
        ei, _eq = _lib.code_e5a(int(prn))
        r = band_acquire.acquire(iq, sample_rate, ei.astype(float),
                                 chip_hz=_L5_CHIP_HZ, code_len=_L5_LEN,
                                 dopp_step=100.0, nperiods=1)
        if r["metric_db"] > _L5_ACQ_DB:
            acq[("E", prn)] = r
    for prn in navic_scan:
        ni = _lib.code_navic(int(prn))
        # NavIC L5-SPS: 1023 chips @ 1.023 Mcps, one order of magnitude
        # narrower than I5/E5a-I -- its own chip_hz/code_len, same 1 ms
        # coherent / single-period acquisition otherwise.
        r = band_acquire.acquire(iq, sample_rate, ni.astype(float),
                                 chip_hz=_NAVIC_CHIP_HZ, code_len=_NAVIC_LEN,
                                 dopp_step=100.0, nperiods=1)
        if r["metric_db"] > _L5_ACQ_DB:
            acq[("I", prn)] = r
    for prn in bds_scan:
        bd, _bp = _lib.code_b2a(int(prn))
        r = band_acquire.acquire(iq, sample_rate, bd.astype(float),
                                 chip_hz=_L5_CHIP_HZ, code_len=_L5_LEN,
                                 dopp_step=100.0, nperiods=1)
        if r["metric_db"] > _L5_ACQ_DB:
            acq[("C", prn)] = r

    decoded, nav_decode = {}, {}
    for (sysc, prn), r in list(acq.items()):
        key = f"{sysc}{prn}"
        try:
            if sysc == "E":
                sym = fnav_decode.demod_symbols_e5a(
                    iq, sample_rate, prn, dopp_hz=r["doppler_hz"],
                    code_phase_chips=r["code_phase_chips"])
                msgs = fnav_decode.decode_messages(sym.tolist())
                own = [m for m in msgs if m["crc_ok"]]
                # Only F/NAV word type 1 carries an SVID field (types 2-4
                # do not, per the ICD); cross-check it when present and
                # otherwise trust the channel's own despreading code.
                t1 = {m["prn"] for m in own
                      if m["type"] == 1 and m.get("prn") is not None}
                if t1 and prn not in t1:
                    nav_decode[key] = "wrong-PRN word1"
                    continue
                if not ({1, 2, 3} <= {m["type"] for m in own}):
                    nav_decode[key] = "no own-PRN 1/2/3"
                    continue
                rec = fnav_decode.reconstruct_ephemeris(own)
            elif sysc == "I":
                sym = navic_decode.demod_symbols(
                    iq, sample_rate, prn, dopp_hz=r["doppler_hz"],
                    code_phase_chips=r["code_phase_chips"])
                hits = navic_decode.find_subframes(sym.tolist())
                # Subframes 1/2 carry no PRN field of their own (NavIC's
                # PRN ID lives only in subframe 3/4 messages, out of
                # scope here); trust the channel's own despreading code,
                # the same fallback E5a-I uses when word 1 is absent.
                if not ({1, 2} <= {h["subframe_id"] for h in hits}):
                    nav_decode[key] = "no subframe 1/2"
                    continue
                rec = navic_decode.reconstruct_ephemeris(hits)
            elif sysc == "C":
                sym = bcnav2_decode.demod_symbols_b2a(
                    iq, sample_rate, prn, dopp_hz=r["doppler_hz"],
                    code_phase_chips=r["code_phase_chips"])
                msgs = bcnav2_decode.decode_messages(sym.tolist())
                own = [m for m in msgs if m["crc_ok"] and m["prn"] == prn]
                if not ({10, 11, 30} <= {m["type"] for m in own}):
                    nav_decode[key] = "no own-PRN 10/11/30"
                    continue
                rec = bcnav2_decode.reconstruct_ephemeris(own)
                # B-CNAV2 broadcasts toe/toc on BDT (GPS - 14 s); this
                # receiver's geometry runs on GPS time.
                for _k in ("toe", "toc"):
                    rec[_k] = (rec[_k] - nav_encoders.BDT_MINUS_GPS_S) % 604800.0
            else:
                sym = cnav_decode.demod_symbols_l5(
                    iq, sample_rate, prn, dopp_hz=r["doppler_hz"],
                    code_phase_chips=r["code_phase_chips"])
                msgs = cnav_decode.decode_messages(sym.tolist())
                own = [m for m in msgs if m["crc_ok"] and m["prn"] == prn]
                if not ({10, 11, 30} <= {m["type"] for m in own}):
                    nav_decode[key] = "no own-PRN 10/11/30"
                    continue
                rec = cnav_decode.reconstruct_ephemeris(own)
            rec["prn"] = prn
            decoded[(sysc, prn)] = rec
            nav_decode[key] = "ok"
        except (ValueError, IndexError):
            nav_decode[key] = "decode fail"

    if len(decoded) < 4:
        return {"error": f"only {len(decoded)} SVs decoded on L5",
                "prns_used": sorted(f"{s}{p}" for s, p in decoded),
                "nav_decode": nav_decode}

    sat_pos, pr, usable = {}, {}, []
    for sysc, prn in sorted(decoded):
        rec = decoded[(sysc, prn)]
        key = f"{sysc}{prn}"
        if sysc == "E":
            svb = geometry.group_delay_bias_s("E", "L5", rec)
            sig, code = e5a, _lib.code_e5a(int(prn))[0]
            chip_hz, code_len = _L5_CHIP_HZ, _L5_LEN
        elif sysc == "I":
            # Single-frequency SPS: TGD applied directly, no ISC term (no
            # second civil frequency broadcast in subframes 1/2), matching
            # Galileo's own single-frequency convention above.
            svb = -rec.get("tgd", 0.0)
            sig, code = navic, _lib.code_navic(int(prn))
            chip_hz, code_len = _NAVIC_CHIP_HZ, _NAVIC_LEN
        elif sysc == "C":
            # BDS-SIS-ICD-B2a-1.0 group delay: B2a's own TGD plus the
            # data/pilot inter-signal correction, same sign convention as
            # GPS's -Tgd+ISC above (documented choice -- the real ICD's
            # sign rule for TGD_B2ap/ISC_B2ad was not independently
            # confirmed, see bcnav2_decode.py).
            svb = -rec.get("tgd_b2ap", 0.0) + rec.get("isc_b2ad", 0.0)
            sig, code = b2a, _lib.code_b2a(int(prn))[0]
            chip_hz, code_len = _L5_CHIP_HZ, _L5_LEN
        else:
            svb = geometry.group_delay_bias_s(sysc, "L5", rec)
            sig, code = l5, _lib.code_l5(int(prn))[0]
            chip_hz, code_len = _L5_CHIP_HZ, _L5_LEN
        o = geometry.observables(rec, approx_rx, approx_time_gps,
                                 signal=sig, sv_clock_bias_s=svb)
        if o["el_deg"] < 5.0:
            nav_decode[key] = "low el"
            continue
        pos, _, _, clk = geometry.solve_transmit_time(rec, approx_rx,
                                                      approx_time_gps)
        mpc = config.C / chip_hz
        cp = band_acquire.fine_code_phase(
            iq, sample_rate, code.astype(float), chip_hz=chip_hz,
            code_len=code_len, dopp_hz=acq[(sysc, prn)]["doppler_hz"])
        err_c = ((cp - o["code_phase_chips"] + code_len / 2) % code_len
                 ) - code_len / 2
        if abs(err_c) > 0.45 * code_len:
            nav_decode[key] = "unaligned"
            continue
        sat_pos[(sysc, prn)] = pos
        pr[(sysc, prn)] = (o["pseudorange_m"] + err_c * mpc
                           + config.C * (clk + svb))
        usable.append((sysc, prn))

    if len(usable) < 4:
        return {"error": f"only {len(usable)} SVs usable on L5",
                "prns_used": sorted(f"{s}{p}" for s, p in usable),
                "nav_decode": nav_decode}

    sol = solve_position(pr, sat_pos, x0=[*approx_rx, 0.0])
    lat, lon, h = _ecef_to_llh(*sol["ecef"])
    entries = []
    for k in usable:
        los = sat_pos[k] - np.array(sol["ecef"])
        entries.append({"_los": (los / np.linalg.norm(los)).tolist()})
    out = {
        "ecef": sol["ecef"], "llh": [lat, lon, h],
        "clock_bias_s": sol["clock_bias_s"],
        "prns_used": sorted(f"{s}{p}" for s, p in usable),
        "pdop": geometry.dop(entries, sol["ecef"])["pdop"],
        "residual_rms_m": sol["residual_rms_m"], "nav_decode": nav_decode,
    }
    if marker_llh:
        truth = np.array(geometry.llh_to_ecef(*marker_llh))
        out["error_m"] = float(np.linalg.norm(np.array(sol["ecef"]) - truth))
    return out


_GLO_CODE_LEN = 511
_GLO_CHIP_HZ = 511_000.0
_GLO_G2_NOMINAL_HZ = 1_246_000_000.0
_GLO_G2_STEP_HZ = 437_500.0


def _fix_from_iq_glo_l2of(iq_path, sample_format, sample_rate, eph_by_prn,
                          approx_time_gps, marker_llh) -> dict:
    """GLONASS L2OF closed-loop fix: acquire the shared 511-chip m-sequence
    at each of the 14 FDMA channel offsets, demodulate the 100 sym/s
    meander stream, decode strings via Hamming-sync (GLONASS has no fixed
    preamble), reconstruct each slot's broadcast state vector and solve.

    GLONASS is FDMA, not CDMA: there is no PRN to scan, only a channel
    number k in [-7, 6] -- each acquired channel is one satellite (or
    none), so results are keyed by k throughout (reported as ``f"R{k}"``).
    ``eph_by_prn`` is accepted for interface symmetry with the other bands
    but unused: there is no broadcast almanac to seed a channel scan from,
    so it is always blind.

    The recovered state vector is referenced to the broadcast t_b (string
    2): the engine puts a GLONASS record's ``toe_ref`` on the 15 min
    Moscow-time grid (ephemeris.align_epochs), so the epoch is decoded from
    t_b and resolved against ``approx_time_gps``.
    """
    from backend.analysis import band_acquire
    from backend.analysis import glo_str_decode as gsd
    from backend.synth import glonass
    from backend.synth import signals as _sig
    from backend.synth.engine import _glo_g1_code

    l2of = _sig.SIGNALS["GLO_L2OF"]
    code = _glo_g1_code().astype(np.float64)
    m_per_chip = config.C / _GLO_CHIP_HZ

    # Strings follow Moscow time (glo_str_encode.nav_stream): strings 1-4
    # are the first 8 s of every 30 s frame. 16 s covers them for a capture
    # that starts on a frame boundary (the closed-loop scenarios do); a
    # capture starting mid-frame needs up to 38 s.
    iq = inspector.read_iq(iq_path, sample_format,
                           max_samples=int(sample_rate * 16.0))
    approx_rx = (np.array(geometry.llh_to_ecef(*marker_llh))
                if marker_llh else np.zeros(3))

    acq = {}
    for k in range(-7, 7):
        off_hz = _sig.glo_channel_offset_hz(k, step_hz=_GLO_G2_STEP_HZ)
        r = band_acquire.acquire(iq, sample_rate, code, chip_hz=_GLO_CHIP_HZ,
                                 code_len=_GLO_CODE_LEN, center_hz=off_hz,
                                 dopp_step=50.0, nperiods=10)
        if r["metric_db"] > 11.0:
            acq[k] = r

    decoded, nav_decode = {}, {}
    for k, r in list(acq.items()):
        off_hz = _sig.glo_channel_offset_hz(k, step_hz=_GLO_G2_STEP_HZ)
        try:
            sym = gsd.demod_symbols(iq, sample_rate,
                                    nominal_ctr_hz=_GLO_G2_NOMINAL_HZ,
                                    offset_hz=off_hz, dopp_hz=r["doppler_hz"],
                                    code_phase_chips=r["code_phase_chips"])
            frame = gsd.sync_and_decode(sym.tolist())
            if not ({1, 2, 3} <= set(frame)):
                nav_decode[k] = "no strings 1-3"
                continue
            rec = gsd.G.reconstruct_ephemeris(frame, toe_ref=approx_time_gps,
                                              glo_k=k)
            if "tb" in rec:
                # the state is referenced to the broadcast t_b (15 min grid)
                rec["toe_ref"] = gsd.G.toe_ref_from_tb(rec["tb"], approx_time_gps)
            decoded[k] = rec
            nav_decode[k] = "ok"
        except (ValueError, IndexError):
            nav_decode[k] = "decode fail"

    def _key(k):
        return f"R{k}"

    if len(decoded) < 4:
        return {"error": f"only {len(decoded)} GLONASS channels decoded on L2OF",
                "prns_used": sorted(_key(k) for k in decoded),
                "nav_decode": {_key(k): v for k, v in nav_decode.items()}}

    sat_pos, pr, usable = {}, {}, []
    for k in sorted(decoded):
        rec = decoded[k]
        state_fn = glonass.glonass_state(rec)
        o = geometry.observables(state_fn, approx_rx, approx_time_gps,
                                 signal=l2of, sv_clock_bias_s=0.0)
        if o["el_deg"] < 5.0:
            nav_decode[k] = "low el"
            continue
        pos, _, _, clk = geometry.solve_transmit_time(state_fn, approx_rx,
                                                       approx_time_gps)
        off_hz = _sig.glo_channel_offset_hz(k, step_hz=_GLO_G2_STEP_HZ)
        cp = band_acquire.fine_code_phase(
            iq, sample_rate, code, chip_hz=_GLO_CHIP_HZ,
            code_len=_GLO_CODE_LEN, dopp_hz=acq[k]["doppler_hz"],
            center_hz=off_hz)
        err_c = ((cp - o["code_phase_chips"] + _GLO_CODE_LEN / 2)
                 % _GLO_CODE_LEN) - _GLO_CODE_LEN / 2
        if abs(err_c) > 0.45 * _GLO_CODE_LEN:
            nav_decode[k] = "unaligned"
            continue
        sat_pos[k] = pos
        pr[k] = o["pseudorange_m"] + err_c * m_per_chip + config.C * clk
        usable.append(k)

    if len(usable) < 4:
        return {"error": f"only {len(usable)} GLONASS channels usable on L2OF",
                "prns_used": sorted(_key(k) for k in usable),
                "nav_decode": {_key(k): v for k, v in nav_decode.items()}}

    sol = solve_position(pr, sat_pos, x0=[*approx_rx, 0.0])
    lat, lon, h = _ecef_to_llh(*sol["ecef"])
    entries = []
    for k in usable:
        los = sat_pos[k] - np.array(sol["ecef"])
        entries.append({"_los": (los / np.linalg.norm(los)).tolist()})
    out = {
        "ecef": sol["ecef"], "llh": [lat, lon, h],
        "clock_bias_s": sol["clock_bias_s"],
        "prns_used": sorted(_key(k) for k in usable),
        "pdop": geometry.dop(entries, sol["ecef"])["pdop"],
        "residual_rms_m": sol["residual_rms_m"],
        "nav_decode": {_key(k): v for k, v in nav_decode.items()},
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
    ``"G2"`` runs the GLONASS L2OF path (see ``_fix_from_iq_glo_l2of``).
    """
    if band == "L2":
        return _fix_from_iq_l2(iq_path, sample_format, sample_rate,
                               eph_by_prn, approx_time_gps, marker_llh)
    if band == "L5":
        return _fix_from_iq_l5(iq_path, sample_format, sample_rate,
                               eph_by_prn, approx_time_gps, marker_llh)
    if band == "G2":
        return _fix_from_iq_glo_l2of(iq_path, sample_format, sample_rate,
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
            svb = geometry.group_delay_bias_s("G", "L1",
                                              eph_by_prn[prn])
            pr[prn] = (e["pseudorange_m"] + err_c * m_per_chip
                       + config.C * (clk + svb))
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
