# backend/geometry.py
from __future__ import annotations

import numpy as np

from backend import config

_A_WGS84 = 6378137.0
_E2_WGS84 = 6.69437999014e-3

# Per-system orbital constants. "G" (GPS) and "J" (QZSS) share the GPS
# WGS84 values so a broadcast dict with no "system" key reproduces today's
# GPS numbers exactly. "E" (Galileo) and "C" (BeiDou) use their own
# datum constants.
SYS_PARAMS = {
    "G": {"mu": config.MU,      "omega_e_dot": config.OMEGA_E_DOT, "f_rel": config.F_REL},
    "J": {"mu": config.MU,      "omega_e_dot": config.OMEGA_E_DOT, "f_rel": config.F_REL},
    "E": {"mu": 3.986004418e14, "omega_e_dot": 7.2921151467e-5,    "f_rel": -4.442807309e-10},
    # BeiDou's toe/toc are carried on the GPS scale internally (BDT + 14 s) but the
    # ICD's earth-rotation term uses toe on BDT: toe_offset_s is that difference.
    "C": {"mu": 3.986004418e14, "omega_e_dot": 7.2921150e-5,       "f_rel": -4.442807309e-10,
          "toe_offset_s": 14.0},
    # NavIC broadcasts a GPS-like Kepler element set on the GPS WGS84 datum
    # (IRNSS ICD sec 6.2.1) -- same constants as "G"/"J", own key so a
    # broadcast dict tagged system="I" resolves without falling back to G.
    "I": {"mu": config.MU,      "omega_e_dot": config.OMEGA_E_DOT, "f_rel": config.F_REL},
}

# BeiDou GEO/IGSO PRNs use the GEO reference-frame rotation (BDS-SIS-ICD-B1I).
_BDS_GEO_PRNS = frozenset(range(1, 6)) | frozenset(range(59, 64))
_BDS_GEO_INC_RAD = 0.15


def llh_to_ecef(lat_deg: float, lon_deg: float, h_m: float) -> tuple[float, float, float]:
    lat = np.radians(lat_deg)
    lon = np.radians(lon_deg)
    n = _A_WGS84 / np.sqrt(1.0 - _E2_WGS84 * np.sin(lat) ** 2)
    x = (n + h_m) * np.cos(lat) * np.cos(lon)
    y = (n + h_m) * np.cos(lat) * np.sin(lon)
    z = (n * (1.0 - _E2_WGS84) + h_m) * np.sin(lat)
    return float(x), float(y), float(z)


def _kepler_E(m: float, e: float) -> float:
    E = m
    for _ in range(30):
        dE = (E - e * np.sin(E) - m) / (1.0 - e * np.cos(E))
        E -= dE
        if abs(dE) < 1e-13:
            break
    return E


def _toe_offset(eph: dict) -> float:
    """Seconds by which the record's ``toe`` runs ahead of the system's own
    time scale (BeiDou: 14 s, GPS scale vs BDT; 0 for every other system)."""
    sysp = SYS_PARAMS.get(eph.get("system", "G"), SYS_PARAMS["G"])
    return sysp.get("toe_offset_s", 0.0)


def _orbit(eph: dict, tk: float, mu: float = config.MU,
          omega_e_dot: float = config.OMEGA_E_DOT, toe_offset: float = 0.0):
    A = eph["sqrtA"] ** 2
    n0 = np.sqrt(mu / A ** 3)
    n = n0 + eph["delta_n"]
    M = eph["m0"] + n * tk
    E = _kepler_E(M, eph["e"])
    sinE, cosE = np.sin(E), np.cos(E)
    nu = np.arctan2(np.sqrt(1 - eph["e"] ** 2) * sinE, cosE - eph["e"])
    phi = nu + eph["omega"]
    s2, c2 = np.sin(2 * phi), np.cos(2 * phi)
    u = phi + eph["cus"] * s2 + eph["cuc"] * c2
    r = A * (1 - eph["e"] * cosE) + eph["crs"] * s2 + eph["crc"] * c2
    i = eph["i0"] + eph["idot"] * tk + eph["cis"] * s2 + eph["cic"] * c2
    xp = r * np.cos(u)
    yp = r * np.sin(u)
    Omega = (eph["omega0"] + (eph["omega_dot"] - omega_e_dot) * tk
             - omega_e_dot * (eph["toe"] - toe_offset))
    return xp, yp, i, Omega, E


def _ecef_from_orbit(xp, yp, i, Omega):
    cO, sO = np.cos(Omega), np.sin(Omega)
    ci, si = np.cos(i), np.sin(i)
    x = xp * cO - yp * ci * sO
    y = xp * sO + yp * ci * cO
    z = yp * si
    return np.array([x, y, z])


def _bds_geo_ecef(xp, yp, i, Omega_geo, wdot_tk):
    """BeiDou GEO/IGSO position, per BDS-SIS-ICD-B1I section 5.2.4.12.

    GEO satellites are propagated with the node ``Omega_geo`` that keeps the
    full ``omega_dot * tk`` term (no ``-omega_e_dot * tk`` earth-rotation
    subtraction), giving a position in a custom inertial-like frame. That
    frame is carried into CGCS2000 by ``Rz(omega_e_dot * tk) . Rx(-5 deg)``,
    where ``Rx`` / ``Rz`` are the ICD coordinate (frame) rotation matrices.
    """
    xg, yg, zg = _ecef_from_orbit(xp, yp, i, Omega_geo)
    phi = np.radians(-5.0)
    cx, sx = np.cos(phi), np.sin(phi)
    # Rx(-5 deg): frame rotation about X
    x1 = xg
    y1 = cx * yg + sx * zg
    z1 = -sx * yg + cx * zg
    cz, sz = np.cos(wdot_tk), np.sin(wdot_tk)
    # Rz(omega_e_dot * tk): frame rotation about Z
    return np.array([cz * x1 + sz * y1, -sz * x1 + cz * y1, z1])


def _sat_pos(eph: dict, t_gps: float):
    """Compute satellite position and orbital parameters from broadcast ephemeris.

    Evaluates the Keplerian orbit exactly once (1 call to ``_orbit``) and returns
    ECEF position (handling BeiDou GEO rotation when appropriate), eccentric anomaly
    ``E``, time offset ``tk``, and orbital constants.
    """
    sysp = SYS_PARAMS.get(eph.get("system", "G"), SYS_PARAMS["G"])
    mu, wdot, frel = sysp["mu"], sysp["omega_e_dot"], sysp["f_rel"]
    tk = t_gps - eph["toe"]
    if tk > 302400:
        tk -= 604800
    elif tk < -302400:
        tk += 604800
    is_bds_geo = eph.get("system") == "C" and (
        eph.get("prn") in _BDS_GEO_PRNS or abs(eph["i0"]) < _BDS_GEO_INC_RAD)

    xp, yp, i, Omega, E = _orbit(eph, tk, mu, wdot, _toe_offset(eph))
    if is_bds_geo:
        pos = _bds_geo_ecef(xp, yp, i, Omega + wdot * tk, wdot * tk)
    else:
        pos = _ecef_from_orbit(xp, yp, i, Omega)
    return pos, E, tk, mu, wdot, is_bds_geo, frel


def sat_state(eph: dict, t_gps: float):
    """Return (pos_ecef, vel_ecef, clk_bias_s) for a broadcast ephemeris record."""
    pos, E, tk, mu, wdot, is_bds_geo, frel = _sat_pos(eph, t_gps)

    def _p(ttk):
        xp, yp, i, Omega, _E = _orbit(eph, ttk, mu, wdot, _toe_offset(eph))
        if is_bds_geo:
            return _bds_geo_ecef(xp, yp, i, Omega + wdot * ttk, wdot * ttk)
        return _ecef_from_orbit(xp, yp, i, Omega)

    dt = 0.5
    vel = (_p(tk + dt) - _p(tk - dt)) / (2 * dt)
    tsv = ((t_gps - eph["toc"] + 302400.0) % 604800.0) - 302400.0
    clk = (eph["af0"] + eph["af1"] * tsv + eph["af2"] * tsv ** 2
           + frel * eph["e"] * eph["sqrtA"] * np.sin(E))
    return pos, vel, float(clk)


def keplerian_state(record: dict):
    """Return ``f(t_gps) -> (pos_ecef, vel_ecef, clk_s)`` for a broadcast
    ``record``. ``record`` may carry ``"system"`` (``"G" "J" "E" "C"``);
    absent means ``"G"`` and reproduces the GPS numbers exactly."""
    return lambda t_gps: sat_state(record, t_gps)


def _rotate_z(v: np.ndarray, theta: float) -> np.ndarray:
    c, s = np.cos(theta), np.sin(theta)
    return np.array([c * v[0] + s * v[1], -s * v[0] + c * v[1], v[2]])


def as_state_fn(eph):
    """Normalise a per-satellite state source into ``f(t_gps) -> (pos, vel, clk)``.

    ``eph`` may be a parsed broadcast-ephemeris dict (the historical
    argument -- propagated with the Kepler model in this module) or an
    already-built callable, e.g. from
    ``precise.PreciseEphemerisProvider.state_fn`` (Strategy D). Callers that
    only ever pass dicts are unaffected.
    """
    if callable(eph):
        def f(t_gps: float):
            pos, vel, clk = eph(t_gps)
            return np.asarray(pos, float), np.asarray(vel, float), float(clk)
        return f
    return lambda t_gps: sat_state(eph, t_gps)


def solve_transmit_time(eph, rx_ecef, t_rx: float, *, compute_vel: bool = True):
    """Iteratively solve for transmit time t_tx = t_rx - tof and Sagnac-rotated position.

    Uses a fast position-only iteration with early exit tolerance (|delta_tof| < 1e-11 s,
    typically converging in 2-3 iterations). Velocity is evaluated only once upon convergence
    if compute_vel is True.
    """
    rx = np.asarray(rx_ecef, float)
    if callable(eph):
        state_fn = as_state_fn(eph)
        tof = 0.075
        for _ in range(8):
            t_tx = t_rx - tof
            pos, vel, clk = state_fn(t_tx)
            pos_rot = _rotate_z(pos, config.OMEGA_E_DOT * tof)
            new_tof = float(np.linalg.norm(pos_rot - rx) / config.C)
            if abs(new_tof - tof) < 1e-11:
                tof = new_tof
                break
            tof = new_tof
        return pos_rot, vel, float(tof), float(clk)

    tof = 0.075
    for _ in range(8):
        t_tx = t_rx - tof
        pos, E, tk, mu, wdot, is_bds_geo, frel = _sat_pos(eph, t_tx)
        pos_rot = _rotate_z(pos, config.OMEGA_E_DOT * tof)
        new_tof = float(np.linalg.norm(pos_rot - rx) / config.C)
        if abs(new_tof - tof) < 1e-11:
            tof = new_tof
            break
        tof = new_tof

    tsv = ((t_tx - eph["toc"] + 302400.0) % 604800.0) - 302400.0
    clk = (eph["af0"] + eph["af1"] * tsv + eph["af2"] * tsv ** 2
           + frel * eph["e"] * eph["sqrtA"] * np.sin(E))

    if not compute_vel:
        vel = np.zeros(3, dtype=float)
    else:
        dt = 0.5

        def _p(ttk):
            xp, yp, i, Omega, _E = _orbit(eph, ttk, mu, wdot, _toe_offset(eph))
            if is_bds_geo:
                return _bds_geo_ecef(xp, yp, i, Omega + wdot * ttk, wdot * ttk)
            return _ecef_from_orbit(xp, yp, i, Omega)

        vel = (_p(tk + dt) - _p(tk - dt)) / (2 * dt)

    return pos_rot, vel, float(tof), float(clk)


def ecef_to_llh(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Convert ECEF coordinates (x, y, z) in meters to geodetic (lat_deg, lon_deg, h_m)
    on the WGS-84 ellipsoid using Bowring's / standard iterative inverse.
    """
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    if p < 1e-6:
        lat = np.pi / 2.0 if z > 0 else -np.pi / 2.0
        h = abs(z) - _A_WGS84 * np.sqrt(1.0 - _E2_WGS84)
        return float(np.degrees(lat)), float(np.degrees(lon)), float(h)
    lat = np.arctan2(z, p * (1.0 - _E2_WGS84))
    for _ in range(6):
        n = _A_WGS84 / np.sqrt(1.0 - _E2_WGS84 * np.sin(lat) ** 2)
        h = p / np.cos(lat) - n
        lat = np.arctan2(z, p * (1.0 - _E2_WGS84 * n / (n + h)))
    return float(np.degrees(lat)), float(np.degrees(lon)), float(h)


def _enu(rx_ecef):
    x, y, z = rx_ecef
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    if p < 1e-6:
        lat = np.pi / 2.0 if z > 0 else -np.pi / 2.0
    else:
        lat = np.arctan2(z, p * (1.0 - _E2_WGS84))
        for _ in range(6):
            n = _A_WGS84 / np.sqrt(1.0 - _E2_WGS84 * np.sin(lat) ** 2)
            h = p / np.cos(lat) - n
            lat = np.arctan2(z, p * (1.0 - _E2_WGS84 * n / (n + h)))
    e = np.array([-np.sin(lon), np.cos(lon), 0.0])
    n = np.array([-np.sin(lat) * np.cos(lon), -np.sin(lat) * np.sin(lon), np.cos(lat)])
    u = np.array([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)])
    return e, n, u


def observables(eph, rx_ecef, t_rx: float, rx_vel=(0.0, 0.0, 0.0),
                atmo_delay_fn=None, rx_clock_range_m: float = 0.0,
                mp_code_bias_m: float = 0.0, *, signal=None,
                sv_clock_bias_s: float = 0.0) -> dict:
    """``eph`` is a broadcast-ephemeris dict or a ``state_fn`` (see
    ``as_state_fn``). Everything downstream -- az/el, geometric range,
    Doppler, pseudorange, code phase -- is identical for both.

    ``atmo_delay_fn``, when given, is ``f(az_rad, el_rad) -> extra one-way
    path delay in metres`` (see ``backend.models.atmosphere``). It is added to the
    pseudorange and code phase exactly once; ``geo_range_m`` and the
    Doppler stay geometric. Legacy callers pass nothing and are unaffected.

    ``rx_clock_range_m`` is a common receiver-clock range bias (metres,
    same for every satellite at one epoch; see ``backend.models.receiver_clock``);
    ``mp_code_bias_m`` is the multipath code-tracking bias for this line of
    sight (see ``backend.models.multipath``). Both default to 0 -- no effect.
    """
    carrier_hz = config.L1_HZ if signal is None else signal.carrier_hz
    chip_hz = config.CA_CHIP_HZ if signal is None else signal.chip_rate_hz
    code_len = config.CA_CODE_LEN if signal is None else signal.code_len

    rx = np.asarray(rx_ecef, float)
    pos, vel, tof, clk = solve_transmit_time(eph, rx, t_rx)
    los_vec = pos - rx
    geo = float(np.linalg.norm(los_vec))
    los = los_vec / geo
    e, n, u = _enu(rx)
    az = (np.degrees(np.arctan2(los @ e, los @ n))) % 360.0
    el = np.degrees(np.arcsin(np.clip(los @ u, -1, 1)))
    v_rel = vel - np.asarray(rx_vel, float)
    fd = -carrier_hz * (v_rel @ los) / config.C
    pr = geo - config.C * (clk + float(sv_clock_bias_s))
    atmo_m = 0.0
    if atmo_delay_fn is not None:
        atmo_m = float(atmo_delay_fn(np.radians(az), np.radians(el)))
        pr += atmo_m
    rx_clk_m = float(rx_clock_range_m)
    mp_m = float(mp_code_bias_m)
    pr += rx_clk_m + mp_m
    code_phase = (pr / config.C * chip_hz) % code_len
    return {
        "az_deg": float(az), "el_deg": float(el), "geo_range_m": geo,
        "pseudorange_m": float(pr), "code_phase_chips": float(code_phase),
        "carrier_doppler_hz": float(fd),
        "code_doppler_hz": float(fd * chip_hz / carrier_hz),
        "sv_clock_bias_s": float(sv_clock_bias_s),
        "atmo_delay_m": atmo_m,
        "rx_clock_range_m": rx_clk_m,
        "multipath_code_bias_m": mp_m,
        "_los": los.tolist(),
    }


def constellation(eph_by_prn: dict, rx_ecef, t_rx: float,
                  mask_deg: float = 5.0, atmo_delay_fn=None,
                  rx_clock_range_m: float = 0.0,
                  mp_code_bias_m: float = 0.0) -> list[dict]:
    """``eph_by_prn`` maps PRN -> broadcast dict or state_fn (see
    ``as_state_fn``); mixing the two across PRNs is allowed.

    ``atmo_delay_fn`` / ``rx_clock_range_m`` / ``mp_code_bias_m`` are
    forwarded to ``observables`` (all default to no effect)."""
    out = []
    for prn in sorted(eph_by_prn):
        o = observables(eph_by_prn[prn], rx_ecef, t_rx,
                        atmo_delay_fn=atmo_delay_fn,
                        rx_clock_range_m=rx_clock_range_m,
                        mp_code_bias_m=mp_code_bias_m)
        if o["el_deg"] >= mask_deg:
            o["prn"] = prn
            out.append(o)
    return out


def state_fn_for(record: dict):
    """Dispatch a broadcast ``record`` to its state function by
    ``record["system"]``. ``G J E C I`` use the Kepler model; ``R``
    (GLONASS) uses the PZ-90 RK4 integrator in ``backend.synth.glonass``;
    ``S`` (SBAS) uses the ICAO Annex-10 propagator in ``backend.synth.sbas``."""
    s = record.get("system", "G")
    if s in ("G", "J", "E", "C", "I"):
        return keplerian_state(record)
    if s == "S":
        # SBAS has its own ICAO Annex-10 constant-acceleration ECEF
        # propagator -- not the PZ-90 integrator GLONASS uses.
        from backend.synth import sbas
        return sbas.sbas_state(record)
    if s == "R":
        # Lazy import: backend.synth imports backend.geometry, so a module-level
        # import here would create a cycle.
        from backend.synth import glonass
        return glonass.glonass_state(record)
    raise NotImplementedError(s)


def constellation_multi(eph_by_key, rx_ecef, t_rx: float, signal_for,
                        mask_deg: float = 5.0, state_fn_by_key=None) -> list[dict]:
    """Evaluate one correlated epoch across systems. ``eph_by_key`` maps
    ``(sys, prn)`` -> broadcast dict; each returned entry is the
    ``observables`` dict plus ``"sys"``, ``"prn"``, ``"signal_id"``.

    When ``state_fn_by_key`` is given, each visible key's satellite state
    comes from ``state_fn_by_key[key]`` (an ``f(sow) -> (pos, vel, clk)``,
    e.g. a precise-ephemeris interpolant) instead of ``state_fn_for(rec)``.
    ``rec`` may then be a bare ``{"system": sysc}`` stub (plus
    ``{"glo_k": k}`` for ``R``). With ``state_fn_by_key=None`` the
    behaviour is byte-identical to the broadcast-only path."""
    out = []
    for key, rec in eph_by_key.items():
        sys = key[0] if isinstance(key, tuple) else "G"
        prn = key[1] if isinstance(key, tuple) else key
        sigs = signal_for(sys)
        if not isinstance(sigs, (list, tuple)):
            sigs = (sigs,)
        sf = state_fn_by_key[key] if state_fn_by_key is not None else state_fn_for(rec)
        glo_k = rec.get("glo_k") if sys == "R" else None
        for sig in sigs:
            o = observables(sf, rx_ecef, t_rx, signal=sig)
            if o["el_deg"] < mask_deg:
                continue
            o["sys"], o["prn"], o["signal_id"] = sys, prn, sig
            if sys == "R":
                # glo_k (FDMA channel number) lives on the ephemeris record,
                # not the observables dict; carry it so the synth layer can
                # apply the FDMA carrier offset. Missing -> None (SV skipped
                # downstream, explicitly).
                o["glo_k"] = glo_k
            out.append(o)
    return out


def dop(entries: list[dict], rx_ecef) -> dict:
    if len(entries) < 4:
        return {k: float("inf") for k in ("gdop", "pdop", "hdop", "vdop", "tdop")}
    e, n, u = _enu(rx_ecef) if any(rx_ecef) else (
        np.array([1.0, 0, 0]), np.array([0, 1.0, 0]), np.array([0, 0, 1.0]))
    rows = []
    for ent in entries:
        los = np.asarray(ent["_los"], float)
        rows.append([los @ e, los @ n, los @ u, 1.0])
    G = np.array(rows)
    try:
        Q = np.linalg.inv(G.T @ G)
    except np.linalg.LinAlgError:
        return {k: float("inf") for k in ("gdop", "pdop", "hdop", "vdop", "tdop")}
    return {
        "gdop": float(np.sqrt(np.trace(Q))),
        "pdop": float(np.sqrt(Q[0, 0] + Q[1, 1] + Q[2, 2])),
        "hdop": float(np.sqrt(Q[0, 0] + Q[1, 1])),
        "vdop": float(np.sqrt(Q[2, 2])),
        "tdop": float(np.sqrt(Q[3, 3])),
    }


def galileo_clock_shift_s(rec: dict | None, target: str) -> float:
    """Galileo af0'ının ``rec["clk_ref"]`` referansından ``target`` ("E5A" ya
    da "E5B") IF referansına taşınması için eklenecek sabit (saniye).

    IF_a = IF_b + Δ, Δ = BGD(E1,E5a) - BGD(E1,E5b) (OS SIS ICD 5.1.4-5.1.5):
    E5B -> E5A için +Δ, E5A -> E5B için -Δ, aynı referans veya etiket yoksa 0.
    Δ için iki BGD de kayıtta bulunmalı (yalnızca F/NAV'dan çözülmüş kayıtta
    BGD(E1,E5b) yoktur; orada Δ bilinmez ve 0 alınır).
    """
    r = rec or {}
    ref = str(r.get("clk_ref", "")).upper()
    if ref not in ("E5A", "E5B") or ref == target or "tgd_e5a" not in r or "tgd" not in r:
        return 0.0
    try:
        delta = float(r["tgd_e5a"]) - float(r["tgd"])
    except (TypeError, ValueError):
        return 0.0
    if delta != delta:
        return 0.0
    return delta if target == "E5A" else -delta


def group_delay_bias_s(sysc: str, band: str, rec: dict | None) -> float:
    """Single-frequency satellite group-delay term (seconds) that a receiver
    adds to the broadcast clock polynomial for this signal -- the
    ``sv_clock_bias_s`` of :func:`observables`. The generator must put the
    same term into the transmitted code phase, otherwise every receiver that
    applies the broadcast TGD/BGD (all of them) is biased by ``c * TGD``.

    * GPS/QZSS (IS-GPS-200 20.3.3.3.3.2 / 30.3.3.3.1.1, IS-GPS-705):
      L1 C/A ``-TGD``, L2C ``-TGD + ISC_L2C``, L5 I5 ``-TGD + ISC_L5I5``.
    * Galileo (OS SIS ICD 5.1.5): E1 ``-BGD(E1,E5b)``, E5a
      ``-(f_E1/f_E5a)^2 BGD(E1,E5a)``. Records carry BGD(E1,E5b) as ``tgd``
      and BGD(E1,E5a) as ``tgd_e5a``.
    * BeiDou: B1I ``-TGD1``; B2a data ``-TGD_B2ap + ISC_B2ad``.
    * NavIC ``-TGD``. GLONASS / SBAS: none.
    """
    r = rec or {}

    def g(k, d=0.0):
        try:
            v = float(r.get(k, d))
        except (TypeError, ValueError):
            return 0.0
        return 0.0 if v != v else v

    if sysc in ("G", "J"):
        if band == "L2":
            return -g("tgd") + g("isc_l2c")
        if band == "L5":
            return -g("tgd") + g("isc_l5i5")
        return -g("tgd")
    if sysc == "E":
        # Kayıttaki af0..af2 hangi iyonosfer-bağımsız (IF) kombinasyona göre
        # verilmişse (clk_ref), yayın zamanlaması da o referansa göre
        # kurulmalı: IF_a = IF_b + Δ, Δ = BGD(E1,E5a) - BGD(E1,E5b).
        #   * clk_ref "E5B" (I/NAV): E1 = af0 - BGD_b (düzeltme yok),
        #     E5a = af0 + Δ - γ*BGD_a.
        #   * clk_ref "E5A" (F/NAV): E5a = af0 - γ*BGD_a (düzeltme yok),
        #     E1 = af0 - BGD_a = af0 - BGD_b - Δ.
        # clk_ref yoksa eski davranış (Δ uygulanmaz) korunur.
        d = galileo_clock_shift_s(r, "E5A" if band == "L5" else "E5B")
        if band == "L5":
            return -((config.L1_HZ / config.L5_HZ) ** 2) * g("tgd_e5a", g("tgd")) + d
        return -g("tgd") + d
    if sysc == "C":
        if band == "L5":
            return -g("tgd_b2ap", g("tgd")) + g("isc_b2ad")
        return -g("tgd")
    if sysc == "I":
        return -g("tgd")
    return 0.0
