from __future__ import annotations

import ctypes
import dataclasses
import datetime as dt
import functools
import json
import logging
import math
import pathlib

import numpy as np

from backend import config, geometry
from backend.analysis import nav_encoders
from backend.ephem import ephemeris, ephemeris_fit, ephemeris_source
from backend.gpstime import GPSTime
from backend.synth import _lib, bands, pilot_codes, postproc, signals
from backend.synth.fading import MODEL_INT, FadingConfig, Motion
from backend.models import atmosphere, channel_models, receiver_clock as rc_mod
from backend.models import impairments as imp_mod, multipath as mp_mod

# Keplerian toe/toc grid (s) used when re-labelling broadcast records to the
# run epoch: a multiple of every nav message's toe resolution (see
# ephemeris.align_epochs).
_TOE_GRID_S = ephemeris.NATIVE_TOE_GRID_S
# The nav streams start this long before the run's first receive instant so
# the earliest transmit time (sow - ~90 ms, more for GEO/IGSO) is covered.
_NAV_LEAD_S = 1.0
_DEFAULT_ATMOSPHERE = {"ionosphere": "klobuchar", "troposphere": "saastamoinen"}

_log = logging.getLogger(__name__)

_KEPLER_KEYS = ("sqrtA e m0 delta_n omega omega0 omega_dot i0 idot cuc cus crc "
                "crs cic cis toe toc af0 af1 af2").split()


def kepler_struct(eph: dict) -> "_lib.KeplerEph":
    s = _lib.KeplerEph()
    for k in _KEPLER_KEYS:
        setattr(s, k, float(eph[k]))
    s._pad = 0.0
    return s


_QUANT = bands._QUANT  # single source of truth (backend.synth.bands)

# Mixer block size (samples). One SP-B trajectory knot per mixer block, so the
# value handed to ``_lib.attach_trajectory`` as ``knot_samples`` must match the
# ``block_samples`` passed to ``_lib.fill_band`` -- share this constant so the
# two never diverge.
_BLOCK_SAMPLES = 65536

# Constellation int -- matches the synth_code CONSTELLATION dispatch. This is the
# key the mixer/fading use; it is NOT the same as the code_sys passed to
# _lib.code (which selects a code VARIANT). For Galileo they intentionally
# differ: spec.sys == 5 (Galileo) while code_sys == 6 (E1C pilot).
_SYS_INT = {"G": 0, "J": 1, "S": 2, "C": 3, "R": 4, "E": 5, "I": 7}
_E1_IS_PILOT = True     # E1C pilot for acquisition; E1B when nav bits added
# GPS L5 I5 Neuman-Hoffman secondary NH10 = 0b0000110101 (IS-GPS-200
# 3.3.2.4). Chip bit 0 -> +1, 1 -> -1, matching the ranging-code sign
# convention. One NH chip per 1 ms primary code period (1 kHz).
_NH10_CHIPS = tuple(1 if b == "0" else -1 for b in "0000110101")
_NH10_ARR = np.ascontiguousarray(_NH10_CHIPS, dtype=np.int8)
_NH10_ARR.flags.writeable = False
# Galileo E5a-I CS20 secondary (Galileo OS SIS ICD Annex C), same fixed
# 20-chip code for every PRN. Same bit->chip convention as NH10 above. One
# CS20 chip per 1 ms primary code period (1 kHz).
_CS20_CHIPS = tuple(1 if b == "0" else -1 for b in "10000100001011101001")
_CS20_ARR = np.ascontiguousarray(_CS20_CHIPS, dtype=np.int8)
_CS20_ARR.flags.writeable = False
# BeiDou B2a-data 5-chip secondary "00010" (BDS-SIS-ICD-B2a-1.0), same
# fixed code for every PRN. Same bit->chip convention as NH10/CS20 above.
# One chip per 1 ms primary code period (1 kHz) -> 200 Hz symbol rate.
_B2A_SEC5_CHIPS = tuple(1 if b == "0" else -1 for b in "00010")
_B2A_SEC5_ARR = np.ascontiguousarray(_B2A_SEC5_CHIPS, dtype=np.int8)
_B2A_SEC5_ARR.flags.writeable = False

# RINEX per-system satellite numbers -> native synth_code PRN domain.
#   QZSS  Jnn  -> PRN 192+nn  (J01 = 193, native range 193..202)
#   SBAS  Snn  -> PRN 100+nn  (S20 = 120, native range 120..158)
# G/E/C already agree with the native domain; R (GLONASS) is FDMA so its code
# is common to all slots and the number is irrelevant to code generation.
_PRN_OFFSET = {"J": 192, "S": 100}


def _native_prn(sysc: str, prn: int) -> int:
    return prn + _PRN_OFFSET.get(sysc, 0)


def _filter_sbas_entries(entries: list[dict], lat: float, lon: float) -> list[dict]:
    """Filter SBAS entries to the regional provider matching receiver position.
    Commercial receivers (e.g. u-blox) check regional service boundaries and
    reject out-of-region SBAS satellites (e.g. GAGAN/MSAS in Europe/Turkey).
    Also prevents RF transmit power dilution across 9+ competing geostationary SVs.
    """
    sbas_entries = [e for e in entries if e.get("sys") == "S"]
    if len(sbas_entries) <= 2:
        return entries

    regional_prns: set[int] = set()
    n_lon = ((float(lon) + 180.0) % 360.0) - 180.0
    n_lat = float(lat)
    if -30.0 <= n_lon <= 55.0 and 15.0 <= n_lat <= 75.0:
        regional_prns = {120, 123, 124, 126, 136}  # EGNOS (Europe, Turkey, Mid-East)
    elif -170.0 <= n_lon <= -50.0 and 10.0 <= n_lat <= 75.0:
        regional_prns = {131, 133, 135, 138}        # WAAS (North America)
    elif 120.0 <= n_lon <= 160.0 and 20.0 <= n_lat <= 60.0:
        regional_prns = {129, 137}                  # MSAS (Japan)
    elif 60.0 <= n_lon <= 100.0 and 0.0 <= n_lat <= 40.0:
        regional_prns = {127, 128, 132}             # GAGAN (India)
    elif 110.0 <= n_lon <= 180.0 and n_lat < 0.0:
        regional_prns = {122, 144}                  # SouthPAN (Australia/NZ)

    matched = [e for e in sbas_entries if int(e.get("prn", 0)) in regional_prns]
    if not matched:
        sbas_sorted = sorted(sbas_entries, key=lambda e: float(e.get("el_deg", 0.0)), reverse=True)
        matched = sbas_sorted[:2]
    else:
        matched = sorted(matched, key=lambda e: float(e.get("el_deg", 0.0)), reverse=True)[:2]

    kept_sbas = set(id(e) for e in matched)
    return [e for e in entries if e.get("sys") != "S" or id(e) in kept_sbas]


_GLO_G1_CODE = None


def _glo_g1_code():
    """GLONASS C/A ranging code: a single 511-chip m-sequence (common to
    every slot -- GLONASS is FDMA, not CDMA) shared verbatim between G1
    (L1OF) and G2 (L2OF, GLONASS ICD L1/L2 -- same code, different band and
    FDMA step). 9-stage LFSR, polynomial 1 + x^5 + x^9, all-ones seed,
    output tapped at stage 7. Returned as an int8 {-1,+1} array. Cached.
    (Generated in Python: native ``synth_code`` has no GLONASS branch and
    rejects prim_len < 1023.)"""
    global _GLO_G1_CODE
    if _GLO_G1_CODE is None:
        import numpy as np
        reg = [1] * 9
        out = np.empty(511, dtype=np.int8)
        for i in range(511):
            out[i] = 1 if reg[6] else -1
            fb = reg[4] ^ reg[8]
            reg = [fb] + reg[:8]
        _GLO_G1_CODE = out
    return _GLO_G1_CODE


def _route_rx_fn(route, duration_s):
    """``f(t_rel) -> (rx_ecef, rx_vel_ecef)`` for a waypoint ``route``.

    Position is the linearly-interpolated waypoint LLH converted to ECEF
    (``scenario.route_llh_at`` -- the same sampler ``gps-sdr-sim``'s motion
    CSV uses); velocity is a central finite difference (one-sided at the run
    ends). Used per mixer block so the moving receiver's own range rate
    reaches the Doppler.
    """
    import numpy as np
    from backend import scenario

    dur = float(duration_s)

    def pos(t):
        return np.asarray(geometry.llh_to_ecef(
            *scenario.route_llh_at(route, dur, t)), float)

    def f(t):
        lo = max(0.0, t - 0.5)
        hi = min(dur, t + 0.5)
        v = (pos(hi) - pos(lo)) / (hi - lo) if hi > lo else np.zeros(3)
        return pos(t), v

    return f


def _trajectory_knots(state, rx, sow, fs, block_samples, total_samples,
                      chip_rate_hz, carrier_hz, code_phase0_chips, code_len,
                      signal=None, carrier_offset_hz=0.0, rx_fn=None,
                      atmo_fn=None, doppler_scale=1.0, clk_fn=None):
    """SP-B per-mixer-block trajectory knots for one satellite.

    Samples ``geometry.observables`` at every mixer-block boundary and turns
    the evolving geometric range into (a) the instantaneous carrier Doppler
    and total code rate at that boundary and (b) the accumulated carrier
    phase and absolute code phase since the run start. Knot 0 reduces to the
    Phase-1 constant-Doppler seed exactly (``dr == 0``).

    ``state`` is a broadcast-ephemeris dict or a state function (precise
    path); ``carrier_offset_hz`` is the static FDMA/LO offset folded into
    the Phase-1 ``carrier_freq_hz`` (non-zero for GLONASS). The mixer runs
    each block from the knot phase at ``cf[j]`` Hz, so ``cph`` must be the
    integral of the SAME frequency: it includes ``2*pi*offset*t_j`` (without
    it every GLONASS block started with a carrier-phase jump of
    ``2*pi*offset*block_s`` mod 2*pi -- a PLL-breaking discontinuity every
    65536 samples). ``carrier_hz`` is the satellite's true carrier
    (``doppler_scale`` rescales the observables' nominal-band Doppler to
    it). ``atmo_fn(az_rad, el_rad, t_rel) -> (iono_m, tropo_m)`` adds the
    time-varying atmospheric delay: group delay on the code, phase advance
    of the ionosphere on the carrier. ``clk_fn(t_rel) -> (offset_s, rate)``
    is the receiver-clock model: a common range term ``c * offset`` on code
    and carrier alike, and ``-f * rate`` on the carrier Doppler / code rate.
    """
    # A broadcast record dict must be dispatched to its system's propagator
    # (GLONASS/SBAS are not Keplerian); geometry.observables' own as_state_fn
    # only knows the Kepler model. A callable (precise path) passes straight
    # through.
    if not callable(state):
        state = geometry.state_fn_for(state)
    nk = max(2, (int(total_samples) + int(block_samples) - 1)
             // int(block_samples))
    L = int(code_len)
    eff = L - (float(code_phase0_chips) % L)
    ts = (np.arange(nk, dtype=np.int64) * int(block_samples)) / fs
    if rx_fn is not None:
        rv = [rx_fn(float(t)) for t in ts]
        rxs = np.array([np.asarray(p, float) for p, _ in rv])
        vrx = np.array([np.asarray(v, float) for _, v in rv])
    else:
        rxs = np.broadcast_to(np.asarray(rx, float), (nk, 3))
        vrx = np.zeros((nk, 3))
    o = _observables_vec(state, rxs, vrx, sow + ts, signal)
    geo, fd, cd = o["geo_range_m"], o["carrier_doppler_hz"], o["code_doppler_hz"]
    dr = geo - geo[0]
    if atmo_fn is not None:
        at = np.array([atmo_fn(float(a), float(e), float(t)) for a, e, t in
                       zip(np.radians(o["az_deg"]), np.radians(o["el_deg"]), ts)])
        iono_abs, tropo_abs = at[:, 0], at[:, 1]
    else:
        iono_abs = tropo_abs = np.zeros(nk)
    iono = iono_abs - iono_abs[0]
    tropo = tropo_abs - tropo_abs[0]
    if clk_fn is not None:
        ck = np.array([clk_fn(float(t)) for t in ts])
        clk_abs, clk_rate = ck[:, 0], ck[:, 1]
    else:
        clk_abs = clk_rate = np.zeros(nk)
    k0 = clk_abs[0]
    dr_code = dr + iono + tropo + config.C * (clk_abs - k0)
    cf = fd * doppler_scale + carrier_offset_hz - carrier_hz * clk_rate
    # ABSOLUTE carrier phase (a function of GPS time and the absolute
    # carrier range only), not phase relative to this run's start: two
    # runs that abut in time (live segments) then join without a
    # carrier-phase jump. A run-relative phase restarted at 0 every
    # segment, which a receiver PLL sees as a random phase step each
    # second -- cycle slips, flipped data bits, no ephemeris. Cycles are
    # reduced in float64 before scaling to radians.
    cyc = (np.fmod(carrier_offset_hz * (sow + ts), 1.0)
           - np.fmod(carrier_hz / config.C * (geo - iono_abs + tropo_abs), 1.0)
           - np.fmod(carrier_hz * clk_abs, 1.0))
    cph = 2.0 * math.pi * cyc
    cr = chip_rate_hz + cd - chip_rate_hz * clk_rate
    kp = eff + chip_rate_hz * ts - (chip_rate_hz / config.C) * dr_code
    dt_b = int(block_samples) / fs
    if nk > 1:
        # Carrier phase difference wrapped to [-pi, pi)
        d_cph_wrap = (np.diff(cph) + math.pi) % (2.0 * math.pi) - math.pi
        # Restore full integer cycle advance using nominal instantaneous frequency
        n_cycles = np.round(cf[:-1] * dt_b - d_cph_wrap / (2.0 * math.pi))
        d_cph_unwrapped = 2.0 * math.pi * n_cycles + d_cph_wrap
        cph_unwrapped = np.concatenate([[cph[0]], cph[0] + np.cumsum(d_cph_unwrapped)])
        cf = np.append(np.diff(cph_unwrapped) / (2.0 * math.pi * dt_b), cf[-1])
        cr = np.append(np.diff(kp) / dt_b, cr[-1])
    return cf.tolist(), cph.tolist(), cr.tolist(), kp.tolist()


# Satellite states for the trajectory knots are sampled every _SAT_NODE_S
# seconds and interpolated with an _SAT_NODE_PTS-point Lagrange polynomial.
# An orbit's n-th derivative scales as omega^n * r (omega ~ 1.5e-4 rad/s), so
# at 10 s spacing the 8-point interpolation error is far below 1e-9 m -- the
# knots match per-knot geometry.observables calls while evaluating the state
# function ~1000x less often (it was ~70 % of a run's wall time).
_SAT_NODE_S = 10.0
_SAT_NODE_PTS = 8


def _sat_interp(state, t_lo: float, t_hi: float):
    """``f(t_array) -> (pos (n,3), vel (n,3))`` interpolating ``state``
    (``f(t) -> (pos, vel, clk)``) over ``[t_lo, t_hi]``."""
    h, m = _SAT_NODE_S, _SAT_NODE_PTS
    a = t_lo - (m // 2) * h
    n = int(math.ceil((t_hi - a) / h)) + m // 2 + 1
    tn = a + h * np.arange(n)
    pv = [state(float(t)) for t in tn]
    P = np.array([np.asarray(item[0], float) for item in pv])
    V = np.array([np.asarray(item[1], float) for item in pv])
    k = np.arange(m)
    denom = np.array([np.prod([kk - j for j in range(m) if j != kk])
                      for kk in range(m)], float)

    def f(t):
        x = (np.asarray(t, float) - a) / h
        i0 = np.clip(np.floor(x).astype(np.int64) - (m // 2 - 1), 0, n - m)
        u = x - i0                                   # in [m/2-1, m/2)
        d = u[:, None] - k[None, :]                  # (q, m)
        w = np.empty_like(d)
        for kk in range(m):
            w[:, kk] = np.prod(np.delete(d, kk, axis=1), axis=1) / denom[kk]
        idx = i0[:, None] + k[None, :]
        return (np.einsum("qm,qmc->qc", w, P[idx]),
                np.einsum("qm,qmc->qc", w, V[idx]))
    return f


def _ecef_to_lat_lon(x, y, z):
    """Convert ECEF coordinates (x, y, z) to WGS-84 geodetic (lat_rad, lon_rad).
    Supports both scalar floats and numpy arrays."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    z = np.asarray(z, dtype=float)
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    a_wgs84 = getattr(geometry, "_A_WGS84", 6378137.0)
    e2_wgs84 = getattr(geometry, "_E2_WGS84", 6.69437999014e-3)
    lat = np.arctan2(z, p * (1.0 - e2_wgs84))
    for _ in range(6):
        n = a_wgs84 / np.sqrt(1.0 - e2_wgs84 * np.sin(lat) ** 2)
        h = p / np.cos(lat) - n
        lat = np.arctan2(z, p * (1.0 - e2_wgs84 * n / (n + h)))
    lat = np.where(p < 1e-6, np.where(z > 0, np.pi / 2.0, -np.pi / 2.0), lat)
    if lat.ndim == 0:
        return float(lat), float(lon)
    return lat, lon


if not hasattr(geometry, "_ecef_to_lat_lon"):
    geometry._ecef_to_lat_lon = _ecef_to_lat_lon


def _observables_vec(state, rxs, vrx, t_rx, signal):
    """``geometry.observables`` for arrays of receive times ``t_rx`` (n,),
    receiver positions/velocities ``rxs``/``vrx`` (n,3): the same light-time
    iteration, Earth-rotation correction and az/el, on an interpolated
    satellite state. Returns geo_range_m, carrier/code Doppler, az/el."""
    carrier_hz = config.L1_HZ if signal is None else signal.carrier_hz
    chip_hz = config.CA_CHIP_HZ if signal is None else signal.chip_rate_hz
    sat = _sat_interp(state, float(t_rx[0]) - 0.2, float(t_rx[-1]))
    tof = np.full(t_rx.shape, 0.075)
    for _ in range(8):
        pos, vel = sat(t_rx - tof)
        th = config.OMEGA_E_DOT * tof
        c, s = np.cos(th), np.sin(th)
        pos_rot = np.stack([c * pos[:, 0] + s * pos[:, 1],
                            -s * pos[:, 0] + c * pos[:, 1], pos[:, 2]], axis=1)
        tof = np.linalg.norm(pos_rot - rxs, axis=1) / config.C
    los_vec = pos_rot - rxs
    geo = np.linalg.norm(los_vec, axis=1)
    los = los_vec / geo[:, None]
    x, y, z = rxs[:, 0], rxs[:, 1], rxs[:, 2]
    lat, lon = geometry._ecef_to_lat_lon(x, y, z)
    le = -np.sin(lon) * los[:, 0] + np.cos(lon) * los[:, 1]
    ln = (-np.sin(lat) * np.cos(lon) * los[:, 0]
          - np.sin(lat) * np.sin(lon) * los[:, 1] + np.cos(lat) * los[:, 2])
    lu = (np.cos(lat) * np.cos(lon) * los[:, 0]
          + np.cos(lat) * np.sin(lon) * los[:, 1] + np.sin(lat) * los[:, 2])
    fd = -carrier_hz * np.einsum("qc,qc->q", vel - vrx, los) / config.C
    return {"geo_range_m": geo, "carrier_doppler_hz": fd,
            "code_doppler_hz": fd * chip_hz / carrier_hz,
            "az_deg": np.degrees(np.arctan2(le, ln)) % 360.0,
            "el_deg": np.degrees(np.arcsin(np.clip(lu, -1, 1)))}


_I8P = ctypes.POINTER(ctypes.c_int8)
_SQRT_HALF = math.sqrt(0.5)


def _i8(arr):
    """int8 numpy array -> ctypes pointer (caller keeps ``arr`` alive)."""
    return arr.ctypes.data_as(_I8P)


def _plain_spec(entry, gain, code_arr, chip_rate_hz):
    """A data-less, secondary-less SvSpec over ``code_arr`` with the entry's
    geometry (nav / secondary / scaling are set by the caller)."""
    spec = _lib.SvSpec()
    spec.code = _i8(code_arr)
    spec.code_len = int(code_arr.size)
    spec.chip_rate_hz = float(chip_rate_hz)
    spec.carrier_freq_hz = entry["carrier_doppler_hz"]
    spec.carrier_phase0_rad = 0.0
    spec.code_phase0_chips = entry["code_phase_chips"]
    spec.code_doppler_hz = entry["code_doppler_hz"]
    spec.nav_mode = 0
    spec.nav_bits = None
    spec.nav_nbits = 0
    spec.nav_sym_rate_hz = 0.0
    spec.gain = gain
    spec.prn = entry["prn"]
    spec.sys = _SYS_INT[entry["sys"]]
    spec.sub_carrier_hz = 0.0
    spec.sec_code = None
    spec.sec_len = 0
    spec.sec_rate_hz = 0.0
    return spec


def _attach_nav(spec, nav):
    nbuf = nav[0]
    nbits = nav[1]
    sym_rate = nav[2]
    spec.nav_mode = 1
    spec.nav_bits = nbuf
    spec.nav_nbits = nbits
    spec.nav_sym_rate_hz = float(sym_rate)
    return nav[3] if len(nav) > 3 else nbuf


def _attach_sec(spec, chips, rate_hz):
    arr = np.ascontiguousarray(chips, dtype=np.int8)
    spec.sec_code = _i8(arr)
    spec.sec_len = int(arr.size)
    spec.sec_rate_hz = float(rate_hz)
    return arr


@functools.lru_cache(maxsize=64)
def _l5_i5_code(prn: int):
    i5, _ = _lib.code_l5(prn)
    arr = np.ascontiguousarray(i5, dtype=np.int8)
    arr.flags.writeable = False
    return arr


@functools.lru_cache(maxsize=64)
def _e5a_i_code(prn: int):
    ei, _ = _lib.code_e5a(prn)
    arr = np.ascontiguousarray(ei, dtype=np.int8)
    arr.flags.writeable = False
    return arr


@functools.lru_cache(maxsize=64)
def _b2a_d_code(prn: int):
    bd, _ = _lib.code_b2a(prn)
    arr = np.ascontiguousarray(bd, dtype=np.int8)
    arr.flags.writeable = False
    return arr


@functools.lru_cache(maxsize=64)
def _l2c_tdm(prn: int):
    """(cm_tdm[20460], cl_tdm[1534500]) at 1.023 Mcps: CM in even slots,
    CL in odd slots, zero elsewhere. Cached: CL generation is ~0.8 M chips."""
    cm, cl = _lib.code_l2c(prn)
    cm_tdm = np.zeros(2 * cm.size, np.int8)
    cm_tdm[0::2] = cm
    cl_tdm = np.zeros(2 * cl.size, np.int8)
    cl_tdm[1::2] = cl
    cm_tdm.flags.writeable = False
    cl_tdm.flags.writeable = False
    return cm_tdm, cl_tdm


def _pilot_spec_for(entry, gain):
    """Dataless L5-band pilot: GPS/QZSS Q5 + NH20, Galileo E5a-Q + CS100,
    BeiDou B2a-P + its 100-chip Weil secondary; one secondary chip per 1 ms
    primary period, clocked on transmit time like the data component.
    Tagged ``_phase_off`` = pi/2: the run loop puts it in quadrature."""
    sysc, prn = entry["sys"], int(entry["prn"])
    try:
        if sysc in ("G", "J"):
            prim, sec = _lib.code_l5(prn)[1], pilot_codes.NH20
        elif sysc == "E":
            prim, sec = _lib.code_e5a(prn)[1], pilot_codes.e5aq_secondary(prn)
        else:
            prim, sec = _lib.code_b2a(prn)[1], pilot_codes.b2ap_secondary(prn)
    except ValueError as exc:
        return None, f"{sysc}{prn}: pilot code rejected ({exc}), skipped"
    prim = np.ascontiguousarray(prim, dtype=np.int8)
    spec = _plain_spec(entry, gain, prim, 10.23e6)
    sbuf = _attach_sec(spec, sec, 1000.0)
    spec._phase_off = math.pi / 2.0
    return spec, [prim, sbuf]


def _sv_spec_for(entry, gain, nav=None):
    """Turn one ``constellation_multi`` entry into a ready ``_lib.SvSpec`` plus
    the ctypes buffers that must outlive it (``keep``). The full multi-system
    ``run()`` wiring lands in Task 16/17; here it is unit-tested directly."""
    sig = entry["signal_id"]
    sysc = entry["sys"]

    # Dataless L5-band pilots (GPS/QZSS Q5, Galileo E5a-Q, BeiDou B2a-P):
    # registered as their own signals (nav_sym_hz == 0) next to the data
    # component; the run loop puts them in quadrature and splits the power.
    if (getattr(sig, "nav_sym_hz", 0) == 0
            and getattr(sig, "band", "L1") == "L5" and sysc in "GJEC"):
        return _pilot_spec_for(entry, gain)

    # GPS / QZSS L2C: CM and CL chip-by-chip time-multiplexed at 1.023 Mcps
    # (IS-GPS-200 3.3.2.4): even slots CM (10230 chips, 20 ms), odd slots
    # CL (767250 chips, 1.5 s). Built as two components on the same carrier,
    # each zero in the other's slots: CM carries CNAV (one 50 Hz symbol per
    # CM period), CL is dataless and aligned to the 1.5 s X1 epoch through
    # the transmit-time clock (run loop, ``_cl_align``). Geometry is in CM
    # units (511.5 kcps); ``_chip_scale`` doubles it into TDM slots.
    if sysc in ("G", "J") and getattr(sig, "band", "L1") == "L2":
        cm_tdm, cl_tdm = _l2c_tdm(entry["prn"])
        spec = _plain_spec(entry, gain, cm_tdm, 1.023e6)
        keep = [cm_tdm]
        if nav is not None:
            keep.append(_attach_nav(spec, nav))
        spec._chip_scale = 2.0
        cl = _plain_spec(entry, gain, cl_tdm, 1.023e6)
        cl._chip_scale = 2.0
        cl._cl_align = True
        spec._companions = [(cl, [cl_tdm])]
        return spec, keep

    # GPS / QZSS L5: the I5 ranging code at 10.23 Mcps (10230 chips, 1 ms
    # primary period) with the NH10 Neuman-Hoffman secondary (one chip per
    # primary period -> 1 kHz), CNAV riding I5 as one 50 Hz symbol per 20
    # primary periods -- the same message set as L2C. The Q5 dataless
    # pilot is not needed for the data-signal closed-loop fix and is left
    # out (as the CL pilot is on L2C).
    if sysc in ("G", "J") and getattr(sig, "band", "L1") == "L5":
        i5 = _l5_i5_code(entry["prn"])
        spec = _plain_spec(entry, gain, i5, 10.23e6)
        keep = [i5]
        if nav is not None:
            keep.append(_attach_nav(spec, nav))
        sbuf = _attach_sec(spec, _NH10_ARR, 1000.0)
        keep.append(sbuf)
        return spec, keep

    # Galileo E5a-I: the data component at 10.23 Mcps (10230 chips, 1 ms
    # primary period, fixed ICD memory code -- not LFSR-generated) with the
    # CS20 secondary (20 chips, one per primary period -> 1 kHz), F/NAV
    # riding it as one 50 Hz symbol per 20 primary periods. This check must
    # come before the generic ``sysc == "E"`` E1 branch below, which would
    # otherwise also catch E5a entries (both share sysc == "E"). The E5a-Q
    # dataless pilot is not needed for the data-signal closed-loop fix and
    # is left out, mirroring Q5 on L5 and CL on L2C.
    if sysc == "E" and getattr(sig, "band", "L1") == "L5":
        ei = _e5a_i_code(entry["prn"])
        spec = _plain_spec(entry, gain, ei, 10.23e6)
        keep = [ei]
        if nav is not None:
            keep.append(_attach_nav(spec, nav))
        sbuf = _attach_sec(spec, _CS20_ARR, 1000.0)
        keep.append(sbuf)
        return spec, keep

    # BeiDou B2a-data: the data component at 10.23 Mcps (10230 chips, 1 ms
    # primary period, real 13-bit dual-LFSR ranging code -- see
    # beidou_b2a_codes.cpp) with the 5-chip "00010" secondary (one chip per
    # primary period -> 1 kHz -> 200 Hz symbol rate), B-CNAV2 riding it as
    # one 200 Hz symbol per 5 primary periods. Must come before the generic
    # ``sysc == "C"`` B1I branch below, which would otherwise also catch B2a
    # entries (both share sysc == "C"). The B2a pilot component is not
    # needed for the data-signal closed-loop fix and is left out, mirroring
    # Q5/E5a-Q/CL.
    if sysc == "C" and getattr(sig, "band", "L1") == "L5":
        bd = _b2a_d_code(entry["prn"])
        spec = _plain_spec(entry, gain, bd, 10.23e6)
        keep = [bd]
        if nav is not None:
            keep.append(_attach_nav(spec, nav))
        sbuf = _attach_sec(spec, _B2A_SEC5_ARR, 1000.0)
        keep.append(sbuf)
        return spec, keep

    if sysc == "R":
        k = entry.get("glo_k")
        if k is None or (isinstance(k, float) and math.isnan(k)):
            return None, f"GLONASS PRN {entry.get('prn')}: missing glo_k, skipped"
    prim_len = sig.code_len
    if sysc == "E":
        # E1-C dataless pilot (+ CS25 secondary) for ranging; switch to the
        # E1-B data component when an I/NAV stream is modulated onto this SV.
        if nav is not None:
            code_sys = 5          # E1-B data
            sec_len = 0
        else:
            code_sys = 6 if _E1_IS_PILOT else 5
            sec_len = 25 if _E1_IS_PILOT else 0
    elif sysc == "C":
        code_sys = 3
        sec_len = 20
    else:
        code_sys = _SYS_INT[sysc]
        sec_len = 0

    if sysc == "R":
        prim, sec = _glo_g1_code(), None
    else:
        try:
            prim, sec = _lib.code(code_sys, entry["prn"], prim_len, sec_len)
        except ValueError:
            return None, (f"{sysc}{entry['prn']}: code generation rejected "
                          f"(PRN out of range for {sysc}), skipped")
    prim = np.ascontiguousarray(prim, dtype=np.int8)
    spec = _lib.SvSpec()
    spec.code = _i8(prim)
    keep = [prim]
    spec.code_len = sig.code_len
    spec.chip_rate_hz = sig.chip_rate_hz
    spec.carrier_freq_hz = entry["carrier_doppler_hz"]
    if sysc == "R":
        # FDMA: the G1 recorder LO sits at 1602.0 MHz (step 562.5 kHz); G2
        # (L2OF) sits at 1246.0 MHz (step 437.5 kHz) -- a different band,
        # same FDMA idea. This SV's carrier is offset by k * step plus its
        # geometric Doppler.
        step_hz = 437_500.0 if sig.band == "G2" else 562_500.0
        try:
            # GLONASS FDMA frekans kayması (k * step_hz)
            spec.carrier_freq_hz += signals.glo_channel_offset_hz(
                int(entry["glo_k"]), step_hz=step_hz)
        except ValueError as exc:
            return None, f"GLONASS PRN {entry.get('prn')}: geçersiz kanal ({exc}), atlandı"
    spec.carrier_phase0_rad = 0.0
    spec.code_phase0_chips = entry["code_phase_chips"]
    spec.code_doppler_hz = entry["code_doppler_hz"]
    spec.nav_mode = 0
    spec.nav_bits = None
    spec.nav_nbits = 0
    spec.nav_sym_rate_hz = 0.0
    if nav is not None:
        keep.append(_attach_nav(spec, nav))
    spec.gain = gain
    spec.prn = entry["prn"]
    spec.sys = _SYS_INT[sysc]
    spec.sub_carrier_hz = sig.sub_carrier_hz
    if sysc == "E" and sig.sub_carrier_hz > 0.0:
        # CBOC(6,1,1/11) sign: E1-B takes +sc(6,1), E1-C takes -sc(6,1).
        spec._cboc = -1 if code_sys == 6 else 1
    if sec_len and sec is not None:
        sec_arr = np.ascontiguousarray(sec, dtype=np.int8)
        spec.sec_code = _i8(sec_arr)
        spec.sec_len = sec_len
        # B1I NH20: 20 chips, one per 1 ms primary period -> 1000 Hz.
        # E1C CS25: 25 chips, one per 4 ms E1C primary code -> 250 Hz.
        spec.sec_rate_hz = 250.0 if sysc == "E" else 1000.0
        keep.append(sec_arr)
    else:
        spec.sec_code = None
        spec.sec_len = 0
        spec.sec_rate_hz = 0.0
    if sysc == "E" and nav is not None:
        # Galileo E1 OS = (E1-B - E1-C) / sqrt(2) on one carrier (OS SIS ICD
        # 2.3.3): the E1-C pilot + CS25 rides with the E1-B data component,
        # half the power each, so a receiver tracking the pilot finds it.
        try:
            c_prim, c_sec = _lib.code(6, entry["prn"], prim_len, 25)
        except ValueError:
            c_prim = None
        if c_prim is not None:
            spec.gain = gain * _SQRT_HALF
            c_prim = np.ascontiguousarray(c_prim, dtype=np.int8)
            pilot = _plain_spec(entry, -gain * _SQRT_HALF, c_prim,
                                sig.chip_rate_hz)
            pilot.sub_carrier_hz = sig.sub_carrier_hz
            pilot._cboc = -1
            c_sbuf = _attach_sec(pilot, c_sec, 250.0)
            spec._companions = [(pilot, [c_prim, c_sbuf])]
    # spec.fading left zeroed: SvSpec() zero-inits and FadingCfg model 0 = off.
    return spec, keep


# Galileo E1 CBOC(6,1,1/11): the sc(6,1) term has 12 sub-chips per 1.023 MHz
# chip (12.276 MHz), its main lobes reaching ~7 MHz from the carrier. CBOC is
# used only when that fits below Nyquist at the carrier's offset from the
# band centre; otherwise it would alias, so plain BOC(1,1) is used.
_CBOC_HALF_BW_HZ = 7.0e6


def _finish_component(spec, entry, sow, knots):
    """Transmit-time clock and trajectory knots for one mixer component of
    ``entry``. Components may carry (set by ``_sv_spec_for``):

    * ``_phase_off`` -- carrier phase offset (rad), pi/2 for a quadrature
      pilot;
    * ``_chip_scale`` -- component chips per geometry chip (L2C TDM: 2, the
      1.023 Mcps slots of the 511.5 kcps CM geometry);
    * ``_cl_align`` -- index the code on transmit time (chips since the nav
      stream's symbol 0, whose epoch is a multiple of 1.5 s) instead of the
      geometry code phase, for L2C CL whose period exceeds the CM period.
    """
    off = float(getattr(spec, "_phase_off", 0.0))
    scale = float(getattr(spec, "_chip_scale", 1.0))
    _attach_tx_clock(spec, entry, sow)
    spec.tx_chips_offset *= scale
    spec.carrier_phase0_rad += off
    if scale != 1.0:
        spec.code_phase0_chips *= scale
        spec.code_doppler_hz *= scale
    tx_align = spec.tx_chips_offset if getattr(spec, "_cl_align", False) else 0.0
    if tx_align:
        # Mixer code index at sample 0 is -code_phase0 mod L; make it the
        # transmit-time chip count u0 = eff + tx_chips_offset.
        L0 = int(entry["signal_id"].code_len * scale)
        eff = (L0 - spec.code_phase0_chips % L0) % L0
        spec.code_phase0_chips = -(eff + tx_align) % spec.code_len
    if knots is not None:
        cf, cph, cr, kp = knots
        _lib.attach_trajectory(
            spec, _BLOCK_SAMPLES, cf, [p + off for p in cph],
            [r * scale for r in cr], [k * scale + tx_align for k in kp])


def _attach_tx_clock(spec, entry, sow):
    """Lock the mixer's nav-symbol / secondary-code clock to satellite
    transmit time (``SvSpec.tx_time_valid``). At receive time ``sow`` the
    signal left the satellite at ``sow - pseudorange/c`` (SV-clock time);
    the mixer's code phase at sample 0 is ``eff = L - (code_phase mod L)``,
    so ``tx_chips_offset`` maps it onto chips elapsed since the nav stream's
    symbol 0 (which leaves at ``stream_t0_sow``). Without this the symbols
    were stamped on RECEIVER time: TOW off by ``sow mod 6 s`` plus the
    ~70 ms flight time, and bit edges falling mid code period."""
    sig = entry["signal_id"]
    L = int(sig.code_len)
    rate = float(sig.chip_rate_hz)
    t0 = nav_encoders.stream_t0_sow(entry["sys"], sig, sow - _NAV_LEAD_S,
                                    prn=int(entry.get("prn", 0)))
    tau_chips = float(entry["pseudorange_m"]) / config.C * rate
    eff = L - (float(entry["code_phase_chips"]) % L)
    if eff >= L:
        eff -= L
    spec.tx_time_valid = 1
    spec.tx_chips_offset = (sow - t0) * rate - tau_chips - eff


def _el_gain(el_deg: float) -> float:
    """Elevation-dependent receive amplitude taper. Without it every SV comes
    out at exactly equal power (gps-sdr-sim applies an antenna-pattern +
    range taper, so its output has a natural per-SV spread). ~0 dB at
    zenith, ~-6 dB near the 5 deg mask. Returns an amplitude multiplier."""
    el = max(0.0, min(90.0, float(el_deg)))
    p = max(0.15, math.sin(math.radians(el)) ** 0.6)   # relative power
    return float(p ** 0.5)                              # -> amplitude


def _visible_gps(req) -> list[tuple[int, dict]]:
    """Visible GPS PRNs (el_deg >= 5) with observables evaluated once at the
    run START epoch (t = sow), with Doppler held constant over the run -- the
    Phase 1 constant-Doppler approximation. The C kernel anchors
    ``code_phase0_chips`` at sample 0 (run start) and propagates it forward with
    ``code_doppler_hz``, so the phase and its rate must share the start epoch."""
    eph = ephemeris.parse_rinex(req.rinex_path)
    gps_start = req.start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    week, sow = ephemeris.gps_week_and_sow(gps_start)
    eph = ephemeris.align_epochs(eph, week, sow)
    rx = geometry.llh_to_ecef(req.lat, req.lon, req.alt)
    t0 = sow
    out = []
    for prn in sorted(eph):
        o = geometry.observables(eph[prn], rx, t0)
        if o["el_deg"] >= 5.0:
            out.append((prn, o))
    return out


@dataclasses.dataclass
class BandPlan:
    """Per-band mixer inputs, as handed to ``_lib.fill_band``.

    ``svs`` carry ctypes pointers into numpy buffers; ``keep_alive`` holds the
    owning objects, so keep the whole ``BandPlan`` referenced while ``svs`` are
    in use. ``nsv_scale`` is the ``nsv_ref`` gain pre-multiplier already folded
    into every ``sv.gain``.
    """
    id: str
    fs: float
    centre_hz: float
    quant: int
    total_samples: int
    block_samples: int
    svs: list
    keep_alive: list
    nsv_scale: float
    # Geometric elevation (deg) of the SV behind each ``svs`` entry, index-
    # parallel to ``svs`` (both Galileo E1B/E1C channels carry the same value).
    # Informational only (used by backend.fpga.select); never feeds synthesis.
    sv_el_deg: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class EnginePlan:
    bands: list


def plan(req) -> EnginePlan:
    """Everything ``run`` does up to (not including) ``_lib.run_bands``."""
    return _run_impl(req, None, plan_only=True)


def run(req, progress_cb=None) -> pathlib.Path:
    """Synthesize a multi-GNSS run with the native engine. Produces one IQ file
    per RF band (``gpssim.bin`` for L1, ``gpssim_g1.bin`` for the GLONASS G1
    FDMA band) plus ``meta.json``. Returns the output directory.

    GPS-only (``req.systems == ("G",)``) is byte-identical to the Phase-1
    single-band path: one L1 band, one ``gpssim.bin``.
    """
    return _run_impl(req, progress_cb)


def _run_impl(req, progress_cb=None, plan_only=False):
    _lib.load_lib()

    in_memory = bool(getattr(req, "in_memory", False))
    if in_memory or plan_only:
        outdir = None
        mem_buffers = {}
    else:
        created = dt.datetime.now(dt.timezone.utc)
        if getattr(req, "outdir", None) is not None:
            outdir = pathlib.Path(req.outdir)
        else:
            outdir = config.OUT_DIR / created.strftime("%Y%m%dT%H%M%S%f")
        outdir.mkdir(parents=True, exist_ok=True)

    systems = tuple(getattr(req, "systems", None) or ("G",))
    warnings: list[str] = []

    # Dynamic waypoint route: move the receiver along `route` and re-evaluate
    # geometry per mixer block. `rx_fn(t_rel) -> (ecef, vel_ecef)`; None keeps
    # the fixed marker position. A route forces the per-block trajectory path
    # (a moving receiver with frozen geometry is meaningless).
    route = getattr(req, "route", None)
    rx_fn = (functools.lru_cache(maxsize=None)(
        _route_rx_fn(route, req.duration_s)) if route else None)
    if route and not getattr(req, "continuous_doppler", True):
        warnings.append("route set with continuous_doppler=False; per-block "
                        "re-propagation forced on so the receiver can move")

    # All systems align to the one GPS SoW run-start epoch: align_epochs rewrites
    # every Keplerian toc/toe and every R/S toe_ref to `sow`, so GLONASS (which
    # propagates on `t_gps - toe_ref`) lines up on the GPS scale with the rest.
    gps_start = req.start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    week, sow = ephemeris.gps_week_and_sow(gps_start)

    nav_override = getattr(req, "nav_override", None)
    precise_multi = (isinstance(nav_override, dict)
                     and "precise_provider" in nav_override)
    ephemeris_mode = "precise" if precise_multi else "broadcast"

    if precise_multi:
        # Precise (SP3) ephemeris for EVERY requested constellation. The payload
        # carries the provider plus the scenario's own (week, sow); there are no
        # broadcast Keplerian records here, so `constellation_multi` runs on
        # per-key state-fn interpolants over bare `{"system": sysc}` stubs.
        provider = nav_override["precise_provider"]
        p_week = int(nav_override["week"])
        p_sow = float(nav_override["sow"])
        # The payload's (week, sow) win for the geometry epoch and the meta/IQ
        # time base. In production they are derived from the same req.start, but
        # flag a drift rather than silently running two different epochs.
        if p_week != week or abs(p_sow - sow) > 5.0:
            warnings.append(
                f"precise run: payload epoch (week {p_week}, sow {p_sow:.1f}) "
                f"differs from the req.start-derived epoch (week {week}, sow "
                f"{sow:.1f}); the payload values are used")
        req_systems = tuple(nav_override.get("systems") or systems)
        req_set = set(req_systems)
        try:
            sats = [tuple(k) for k in provider.satellites()]
        except Exception:                       # pragma: no cover - defensive
            sats = []
        keys = [k for k in sats if k[0] in req_set]
        covered = {k[0] for k in keys}
        # A missing REQUIRED system still raises on the precise path, same as
        # the broadcast path's require=("G",) (spec: precise-ephemeris-design
        # .md L171). Optional systems still degrade with a warning below.
        if "G" in req_set and "G" not in covered:
            raise ephemeris.EphemerisUnavailable(
                "precise: the SP3 product has no GPS coverage but G is a "
                "required system")
        for s in req_systems:
            if s not in covered:
                warnings.append(f"precise ephemeris has no {s} satellites; "
                                "system omitted from this run")
        state_fns, skipped = ephemeris_source.build_precise_state_fns(
            provider, keys, p_week, p_sow)
        for k in skipped:
            warnings.append(f"precise ephemeris does not cover "
                            f"{k[0]}{k[1]:02d}; satellite omitted")

        # GLONASS is FDMA: the synth layer needs each slot's channel number,
        # which the precise product does not carry -- recover it from the
        # broadcast RINEX. A missing/unusable RINEX drops GLONASS with a
        # warning rather than failing the run.
        glo_k_by_key: dict = {}
        r_eph: dict = {}
        r_keys = [k for k in state_fns if k[0] == "R"]
        if r_keys:
            try:
                r_eph = ephemeris.parse_rinex_multi(req.rinex_path, ("R",),
                                                    require=())
                r_eph = ephemeris.align_epochs(r_eph, week, sow)
            except Exception as exc:            # noqa: BLE001 - degrade, warn
                warnings.append("precise run: GLONASS needs broadcast FDMA "
                                f"channel numbers but {req.rinex_path!r} is "
                                f"unusable ({exc}); GLONASS omitted")
            for k in r_keys:
                rec = r_eph.get(k) or {}
                gk = rec.get("glo_k")
                if gk is None or (isinstance(gk, float) and math.isnan(gk)):
                    warnings.append("precise run: no FDMA channel for GLONASS "
                                    f"R{k[1]:02d}; satellite omitted")
                    state_fns.pop(k, None)
                else:
                    glo_k_by_key[k] = int(gk)

        # Broadcast-compatible nav-message records built from the precise
        # state, one per Keplerian satellite (G/J/E/C/I): a real Gauss-Newton
        # fit (ephemeris_fit, generalised to every system's own
        # geometry.SYS_PARAMS datum) against that satellite's own state_fn,
        # not a bare stub -- so nav_message generation below is not limited
        # to GPS-only the way the older broadcast-precise path is. GLONASS
        # is not Keplerian and reuses the broadcast RINEX record already
        # read above for glo_k. SBAS has neither path; skipped with one
        # warning -- the ranging IQ itself is unaffected either way, only
        # the data-bit stream.
        nav_eph_by_key: dict = {}
        if getattr(req, "nav_message", True):
            # Fit on the toe grid (BDT grid for BeiDou), not at the run
            # start itself -- see ephemeris.align_epochs(kepler_grid_s).
            def _fit_epoch(sc):
                off = 14.0 if sc == "C" else 0.0   # GPST - BDT, see nav_encoders
                return GPSTime(p_week, ((p_sow - off) // _TOE_GRID_S)
                               * _TOE_GRID_S + off)
            fit_source = getattr(provider.product, "source", "precise")
            for k, sfn in state_fns.items():
                sc, pn = k
                if sc in ("G", "J", "E", "C", "I"):
                    try:
                        nav_eph_by_key[k] = ephemeris_fit.fit_satellite(
                            sfn, _fit_epoch(sc), sys=sc, prn=pn,
                            source=fit_source)
                    except Exception as exc:        # noqa: BLE001 - degrade
                        # Both a rejected fit (EphemerisFitError) and the
                        # +/-2h fit window falling outside this satellite's
                        # own SP3 coverage span (precise.PreciseProductError)
                        # land here -- the ranging IQ (state_fns, already
                        # built) is unaffected either way, only this SV's
                        # nav-message data symbol.
                        warnings.append(
                            f"{sc}{pn:02d}: precise->broadcast nav fit "
                            f"failed ({exc}); data symbol left constant")
                elif sc == "R":
                    rec = r_eph.get(k)
                    if rec:
                        nav_eph_by_key[k] = rec
            if any(k[0] == "S" for k in state_fns):
                warnings.append(
                    "precise run: SBAS nav message not generated (not "
                    "Keplerian, own broadcast format); ranging IQ unaffected")

        stubs: dict = {}
        for k in state_fns:
            rec = {"system": k[0]}
            if k[0] == "R":
                rec["glo_k"] = glo_k_by_key[k]
            stubs[k] = rec

        rx = (rx_fn(0.0)[0] if rx_fn is not None
              else geometry.llh_to_ecef(req.lat, req.lon, req.alt))
        entries = geometry.constellation_multi(
            stubs, rx, p_sow, signals.signal_for, mask_deg=5.0,
            state_fn_by_key=state_fns)
        entries.sort(key=lambda e: (e["sys"], e["prn"], e["signal_id"].band))
        for e in entries:
            e["_state"] = state_fns.get((e["sys"], e["prn"]))
            e["_nav_eph"] = nav_eph_by_key.get((e["sys"], e["prn"]))
            e["prn"] = _native_prn(e["sys"], e["prn"])
        entries = _filter_sbas_entries(entries, float(getattr(req, "lat", 0.0)), float(getattr(req, "lon", 0.0)))
        systems = tuple(sorted({e["sys"] for e in entries}))
    else:
        if nav_override:
            # Precise (SP3-fitted) broadcast records: GPS-only, keyed by bare int
            # PRN. There is no precise fit for the other constellations, so a
            # precise run stays GPS-only whatever `systems` asked for.
            extra = [s for s in systems if s != "G"]
            if extra:
                warnings.append("precise ephemeris covers GPS only; ignored "
                                f"systems {extra!r} for this run")
            eph = dict(nav_override)
            systems = ("G",)
            # The precise path was taken (SP3-fitted broadcast records), even
            # though coverage is effectively GPS-only -- provenance reflects
            # which branch ran, not the system count.
            ephemeris_mode = "precise"
        else:
            a_week, a_sow = week, sow
            a_dt = gps_start
            if getattr(req, "eph_epoch", None) is not None:
                a_dt = req.eph_epoch + dt.timedelta(
                    seconds=config.GPS_UTC_LEAP_S)
                a_week, a_sow = ephemeris.gps_week_and_sow(a_dt)
            # GPS zorunlu değil: Talep edilen sistemler arasında 'G' varsa 'G' aranır, yoksa boş demet verilir
            req_req = tuple(s for s in ("G",) if s in systems)
            eph = ephemeris.parse_rinex_multi(req.rinex_path, systems,
                                              require=req_req, at_gps=a_dt)
            got = {(k[0] if isinstance(k, tuple) else "G") for k in eph}
            dropped = [s for s in systems if s not in got]
            if dropped:
                warnings.append(f"RINEX has no records for systems {dropped!r}; "
                                "generated the remaining systems")
                systems = tuple(s for s in systems if s in got)

        if ephemeris_mode != "broadcast":
            a_week, a_sow = week, sow
        # Records whose own epoch covers the run are used as broadcast (the
        # real sky); only stale ones are relabeled onto the toe grid.
        eph = ephemeris.align_epochs(
            eph, a_week, a_sow, kepler_grid_s=_TOE_GRID_S,
            keep_real_within_s=ephemeris.REAL_EPH_WINDOW_S)
        rx = (rx_fn(0.0)[0] if rx_fn is not None
              else geometry.llh_to_ecef(req.lat, req.lon, req.alt))
        req_bands = getattr(req, "bands", None)
        if req_bands:
            def sig_for(s, _b=tuple(req_bands)):
                return signals.signals_for(s, _b)
        else:
            sig_for = signals.signal_for
        entries = geometry.constellation_multi(eph, rx, sow, sig_for,
                                               mask_deg=5.0)
        entries.sort(key=lambda e: (e["sys"], e["prn"], e["signal_id"].band))
        for e in entries:
            e["_state"] = eph.get((e["sys"], e["prn"]), eph.get(e["prn"]))
            e["prn"] = _native_prn(e["sys"], e["prn"])
        entries = _filter_sbas_entries(entries, float(getattr(req, "lat", 0.0)), float(getattr(req, "lon", 0.0)))

    # --- Signal-level terms a real receiver removes using the broadcast
    # message. Every receiver applies the broadcast TGD/BGD, the broadcast
    # Klobuchar ionosphere and a tropospheric model to its pseudoranges, so
    # the transmitted code phase must carry them or the fix is biased by
    # metres (vertical tens of metres at low elevation). Header iono is the
    # single source for BOTH the injected delay and the LNAV/CNAV broadcast.
    try:
        hdr = dict(ephemeris.rinex_header_iono_utc(req.rinex_path) or {})
    except Exception:                       # noqa: BLE001 - degrade
        hdr = {}
    atmo_cfg = atmosphere.AtmosphereConfig.from_dict(
        getattr(req, "atmosphere", None) or _DEFAULT_ATMOSPHERE)
    if atmo_cfg.ionosphere == "klobuchar":
        if hdr.get("iono_alpha") and hdr.get("iono_beta"):
            atmo_cfg.klobuchar_alpha = tuple(float(x) for x in hdr["iono_alpha"][:4])
            atmo_cfg.klobuchar_beta = tuple(float(x) for x in hdr["iono_beta"][:4])
        else:
            hdr["iono_alpha"] = list(atmo_cfg.klobuchar_alpha)
            hdr["iono_beta"] = list(atmo_cfg.klobuchar_beta)
    # Receiver-clock model, opted into the IQ by ``models_to_iq`` exactly as
    # on the gps-sdr-sim path. Here it is exact per satellite and per band
    # (common range term on code and carrier, drift on every Doppler),
    # rather than one quasi-static shift of the composite signal.
    chan = channel_models.ChannelModels.from_request(req)
    to_iq = bool(getattr(req, "models_to_iq", False))
    rc_cfg = chan.receiver_clock
    clk_fn = None
    if to_iq and rc_cfg.enabled:
        def clk_fn(t_rel, _c=rc_cfg):
            return (rc_mod.offset_s(_c, sow + t_rel),
                    rc_mod.rate_s_per_s(_c, sow + t_rel))
    rx_lat_rad = math.radians(float(req.lat))
    rx_lon_rad = math.radians(float(req.lon))
    rx_h_m = float(req.alt)

    def _atmo_for(carrier_hz, sysc=None):
        if not atmo_cfg.enabled:
            return None
        # Galileo I/NAV word 5 ai0..ai2 = 0 yayınlıyor (inav_encode.word_type_5):
        # NeQuick-G alıcı tarafında ~0 iyonosfer düzeltir. Klobuchar gecikmesini
        # E sinyaline gömmek alıcının çıkaramayacağı birkaç-on metre yanlılık
        # (düşük elevasyonda artar) ve GPS ile tutarsız ISB üretirdi -> Galileo
        # fix'i bozar. "Enjekte edilen = yayından çıkarılabilen" ilkesi gereği
        # E için iyonosfer enjekte edilmez (troposfer yine uygulanır).
        use_iono = atmo_cfg.ionosphere == "klobuchar" and sysc != "E"

        def f(az_rad, el_rad, t_rel):
            iono = tropo = 0.0
            if use_iono:
                iono = atmosphere.klobuchar_delay_m(
                    atmo_cfg.klobuchar_alpha, atmo_cfg.klobuchar_beta,
                    sow + t_rel, rx_lat_rad, rx_lon_rad, az_rad, el_rad,
                    freq_hz=carrier_hz)["delay_m"]
            if atmo_cfg.troposphere == "saastamoinen":
                tropo = atmosphere.saastamoinen_delay_m(
                    el_rad, rx_h_m, atmo_cfg.pressure_hpa,
                    atmo_cfg.temperature_k, atmo_cfg.humidity)["delay_m"]
            return iono, tropo
        return f

    for e in entries:
        sig = e["signal_id"]
        sysc = e["sys"]
        rec = e.get("_nav_eph") if precise_multi else e.get("_state")
        rec = rec if isinstance(rec, dict) else None
        f_car = float(sig.carrier_hz)
        scale = 1.0
        gk = e.get("glo_k")
        if sysc == "R" and gk is not None and not (
                isinstance(gk, float) and math.isnan(gk)):
            # GLONASS FDMA: Doppler scales with the slot's own carrier.
            step_hz = 437_500.0 if sig.band == "G2" else 562_500.0
            f_car += int(gk) * step_hz
            scale = f_car / float(sig.carrier_hz)
            e["carrier_doppler_hz"] *= scale
        e["_carrier_hz"] = f_car
        e["_doppler_scale"] = scale
        fa = _atmo_for(f_car, sysc)
        e["_atmo_fn"] = fa
        iono, tropo = (fa(math.radians(e["az_deg"]), math.radians(e["el_deg"]),
                          0.0) if fa is not None else (0.0, 0.0))
        svb = geometry.group_delay_bias_s(sysc, sig.band, rec)
        e["pseudorange_m"] += -config.C * svb + iono + tropo
        if clk_fn is not None:
            c_off, c_rate = clk_fn(0.0)
            e["pseudorange_m"] += config.C * c_off
            e["carrier_doppler_hz"] -= f_car * c_rate
            e["code_doppler_hz"] -= sig.chip_rate_hz * c_rate
        e["code_phase_chips"] = ((e["pseudorange_m"] / config.C
                                  * sig.chip_rate_hz) % sig.code_len)

    # for_run(): a keyed model without a caller key draws a fresh one here,
    # once, so every SV and band of this run shares it.
    cfg = FadingConfig.from_dict(getattr(req, "fading", None)).for_run()
    fading_model_int = MODEL_INT[cfg.model] if cfg.enabled() else 0
    # Channel motion: with no explicit speed, a waypoint route drives the
    # channel by the distance travelled along it (stops fade slowly, fast
    # stretches fast). The ctypes arrays live as long as this call.
    fade_motion = None
    if fading_model_int and cfg.speed_mps is None and route:
        from backend import scenario as _scenario
        fade_motion = Motion(*_scenario.route_distance_profile(
            route, req.duration_s))

    # Broadcast navigation message per system (SP-A GPS LNAV + SP-D others):
    # one +/-1 symbol stream per SV at that system's symbol rate, built from
    # the same broadcast record that drives the IQ geometry. Wired into
    # SvSpec.nav_* for the KnownFrame modulation path in the C++ mixer.
    # Keyed by native PRN so `_sv_spec_for` can look it up directly.
    nav_streams: dict = {}
    nav_prov: dict = {}

    def _nav_prov_name(sysc: str, band: str) -> str:
        if sysc in ("G", "J") and band in ("L2", "L5"):
            return "cnav"          # IS-GPS-200 Sec. 30/40 CNAV
        if sysc == "E" and band == "L5":
            return "fnav"          # Galileo OS SIS ICD Sec. 4.2 F/NAV
        if sysc == "C" and band == "L5":
            return "bcnav2"        # BDS-SIS-ICD-B2a-1.0 Sec. 6 B-CNAV2
        return {"G": "lnav", "J": "lnav", "E": "inav", "C": "d1",
                "R": "strings", "S": "sbas", "I": "navic"}.get(sysc, "on")

    if getattr(req, "nav_message", True):
        gps_prns = [int(x["prn"]) for x in entries if x.get("sys") == "G" and x.get("prn")]
        eph_by_prn = {}
        for x in entries:
            p_x = int(x.get("prn", 0) or 0)
            if p_x:
                rec_x = x.get("_nav_eph") if precise_multi else x.get("_state")
                if isinstance(rec_x, dict):
                    eph_by_prn[p_x] = rec_x
                    sys_x = x.get("sys")
                    if sys_x:
                        eph_by_prn[(sys_x, p_x)] = rec_x

        # LNAV carries a 25-page almanac of ALL 32 GPS SVs, not just the
        # visible ones. Without records for the invisible PRNs the pages went
        # out zero-filled (sqrtA = 0, health = 0: "healthy, orbit at the
        # Earth's centre"), which no real satellite sends.
        try:
            for k_x, rec_x in (eph or {}).items():
                s_x, p_x = (k_x if isinstance(k_x, tuple) else ("G", k_x))
                if s_x == "G" and isinstance(rec_x, dict):
                    eph_by_prn.setdefault(("G", int(p_x)), rec_x)
        except (NameError, TypeError, ValueError):
            pass

        for e in entries:
            sysc = e["sys"]
            # precise_multi: `_state` is a callable state_fn (SP-B trajectory
            # re-propagation), not a broadcast dict -- the fitted/RINEX
            # record built above (`_nav_eph`) is the nav-message source.
            rec = e.get("_nav_eph") if precise_multi else e.get("_state")
            if not isinstance(rec, dict):
                continue
            try:
                res = nav_encoders.nav_stream_for(
                    sysc, e["signal_id"], rec, hdr, week, sow - _NAV_LEAD_S,
                    req.duration_s + _NAV_LEAD_S,
                    eph_by_prn=eph_by_prn,
                    prn=int(e.get("prn", 0)),
                    rx_lat=float(getattr(req, "lat", 0.0)),
                    rx_lon=float(getattr(req, "lon", 0.0)),
                    gps_prns=gps_prns)
            except (KeyError, ValueError) as exc:
                warnings.append(f"{sysc}{e['prn']}: nav-message encode failed "
                                f"({exc}); data symbol left constant")
                continue
            if res is None:
                continue
            arr, sym_rate = res
            nav_arr = np.ascontiguousarray(arr, dtype=np.int8)
            nbuf = _i8(nav_arr)
            band_id = e["signal_id"].band
            # Keyed by (sys, prn, band): native PRN numbering overlaps across
            # systems (e.g. GPS PRN 5 and Galileo PRN 5 both sit at native
            # "5"), and L5 now carries more than one system -- (prn, band)
            # alone would let one system's nav stream clobber another's.
            nav_streams[(sysc, e["prn"], band_id)] = (
                nbuf, len(nav_arr), float(sym_rate), nav_arr)
            nav_prov.setdefault(f"{sysc}/{band_id}",
                                _nav_prov_name(sysc, band_id))
    if not nav_prov:
        nav_prov = "none"

    plans = bands.plan_bands(entries, req)
    if not plans:
        raise RuntimeError("no visible satellites for any band")
    for p in plans:
        if p.fs_note:
            _log.warning("engine.run: %s", p.fs_note)
            warnings.append(p.fs_note)

    band_specs = []
    band_plans = []
    keep_alive = []
    meta_bands = []
    meta_svs = []
    for plan in plans:
        sv_list = []
        sv_el = []
        band_keep = []
        band_sys = set()
        # (sys, prn, band) with a registered dataless pilot entry: data and
        # pilot share the satellite's power equally.
        piloted = {(e["sys"], e["prn"], e["signal_id"].band)
                   for e in plan.entries
                   if getattr(e["signal_id"], "nav_sym_hz", 0) == 0}
        for e in plan.entries:
            el_deg = float(e.get("el_deg", 90.0))
            static_gain = _el_gain(el_deg)
            if (e["sys"], e["prn"], e["signal_id"].band) in piloted:
                static_gain *= _SQRT_HALF
            spec, keep = _sv_spec_for(
                e, static_gain,
                nav=nav_streams.get((e["sys"], e["prn"], e["signal_id"].band)))
            if spec is None:
                _log.warning("engine.run: %s", keep)
                warnings.append(str(keep))
                continue
            comps = [(spec, keep)] + list(getattr(spec, "_companions", ()))
            # Offset of this signal's carrier from the band centre: non-zero
            # for BeiDou B1I (1561.098 MHz) and GPS/Galileo sharing a widened
            # L1 output; GLONASS FDMA offsets are already in carrier_freq_hz.
            if_hz = float(e["signal_id"].carrier_hz) - plan.centre_hz
            if if_hz:
                for c, _k in comps:
                    c.carrier_freq_hz += if_hz
            knots = None
            if ((getattr(req, "continuous_doppler", True) or rx_fn is not None)
                    and e.get("_state") is not None):
                sig0 = e["signal_id"]
                carr_off = spec.carrier_freq_hz - float(e["carrier_doppler_hz"])
                knots = _trajectory_knots(
                    e["_state"], rx, sow, plan.fs, _BLOCK_SAMPLES,
                    int(round(plan.fs * req.duration_s)),
                    sig0.chip_rate_hz, e.get("_carrier_hz", sig0.carrier_hz),
                    e["code_phase_chips"], sig0.code_len,
                    signal=sig0, carrier_offset_hz=carr_off, rx_fn=rx_fn,
                    atmo_fn=e.get("_atmo_fn"),
                    doppler_scale=e.get("_doppler_scale", 1.0),
                    clk_fn=clk_fn)
            # Carrier of this signal (per-channel for GLONASS FDMA): the
            # wavelength that sets the diffuse multipath Doppler spread.
            fade_carrier_hz = float(e.get("_carrier_hz",
                                          e["signal_id"].carrier_hz))
            for c, c_keep in comps:
                _finish_component(c, e, sow, knots)
                if plan.fs >= 2.0 * (abs(if_hz) + _CBOC_HALF_BW_HZ):
                    c.cboc = int(getattr(c, "_cboc", 0))
                if fading_model_int:
                    cfg.fill(c.fading, e["sys"], fade_carrier_hz, el_deg,
                             fade_motion)
                sv_list.append(c)
                sv_el.append(el_deg)
                keep_alive.append(c_keep)
                band_keep.append(c_keep)
            band_sys.add(e["sys"])
            sig = e["signal_id"]
            sv_meta = {"sys": e["sys"], "prn": e["prn"],
                       # A satellite can carry more than one signal_id in the
                       # same run (e.g. GPS L1 C/A and L2C are two separate
                       # entries for the same PRN) -- this field says which
                       # band THIS row is, so a client rendering one row per
                       # (sv, band) does not have to guess it back from `sys`.
                       "band": sig.band,
                       "code_len": sig.code_len,
                       "chip_hz": sig.chip_rate_hz,
                       # carrier offset from the band centre in the IQ
                       # (BeiDou B1I vs GPS/Galileo on a widened L1 output)
                       "if_hz": if_hz,
                       "code_doppler_hz": e["code_doppler_hz"],
                       "el_deg": round(el_deg, 2),
                       "az_deg": round(float(e.get("az_deg", 0.0)), 2),
                       # static elevation amplitude taper applied to this SV;
                       # the channel model (fading_* below) multiplies it by
                       # a time-varying complex gain in the C++ mixer.
                       "gain": round(static_gain, 4),
                       "gain_db": round(20.0 * math.log10(static_gain), 2),
                       # channel model inputs, so a client can reproduce the
                       # exact gain the C++ mixer applied
                       # (backend/synth/native/fading.cpp, frontend/js/
                       # fading.js) at any run time t and show the per-SV
                       # power varying as the IQ is scrubbed.
                       "fading_model": fading_model_int}
            if fading_model_int:
                sv_meta.update({
                    "fading_env": cfg.environment,
                    "fading_speed_mps": cfg.speed_mps,
                    "fading_carrier_hz": fade_carrier_hz,
                    "fading_el_deg": el_deg})
            if fading_model_int == MODEL_INT["seeded"]:
                sv_meta["fading_seed"] = int(cfg.seed)
            if fading_model_int == MODEL_INT["keyed"]:
                # The key stays secret unless the caller asked to keep it;
                # the fingerprint only says which key a run used.
                sv_meta["fading_key_id"] = cfg.key_id()
                if cfg.record_key:
                    sv_meta["fading_key"] = cfg.key.hex()
            gk = e.get("glo_k")
            if e["sys"] == "R" and gk is not None and not (
                    isinstance(gk, float) and math.isnan(gk)):
                sv_meta["glo_k"] = int(gk)
            meta_svs.append(sv_meta)
        if not sv_list:
            continue
        # Opt-in sabit uydu başına güç (SIM_NSV_REF > 0): C++ motoru bileşik
        # seviyeyi 0.25*FS/sqrt(nsv) ile ölçekler; nsv segmentler/run'lar
        # arasında değişince (uydu doğup batınca) her uydunun gücü sıçrar ve
        # alıcının C/N0'sı/AGC'si oynar. sqrt(nsv/nsv_ref) ön çarpanı iki
        # sqrt'yi götürür -> uydu başına genlik 0.25*FS/sqrt(nsv_ref) sabit.
        # Varsayılan 0 = eski davranış (toplam güç sabit). nsv > nsv_ref ise
        # toplam RMS sqrt(nsv/nsv_ref) kadar büyür; kırpma payı için nsv_ref'i
        # tipik en yüksek kanal sayısına yakın seç.
        nsv_ref = float(getattr(req, "nsv_ref", 0.0) or config.SIM_NSV_REF)
        pnorm = 1.0
        if nsv_ref > 0.0:
            pnorm = math.sqrt(len(sv_list) / nsv_ref)
            for c in sv_list:
                c.gain *= pnorm
        total_samples = int(round(plan.fs * req.duration_s))
        band_plans.append(BandPlan(
            id=plan.id, fs=plan.fs, centre_hz=plan.centre_hz, quant=plan.quant,
            total_samples=total_samples, block_samples=_BLOCK_SAMPLES,
            svs=sv_list, keep_alive=band_keep, nsv_scale=pnorm,
            sv_el_deg=sv_el))
        if plan_only:
            continue
        bs = _lib.BandSpec()
        if in_memory:
            raw_dtype = np.int8 if req.sample_format == "int8" else np.int16
            raw_buf = np.empty(total_samples * 2, dtype=raw_dtype)
            _lib.fill_band(bs, None, plan.fs, plan.quant,
                           total_samples, sv_list, block_samples=_BLOCK_SAMPLES,
                           out_mem=raw_buf)
            mem_buffers[plan.out_file] = raw_buf
        else:
            out_p = str(outdir / plan.out_file).encode()
            _lib.fill_band(bs, outdir / plan.out_file, plan.fs, plan.quant,
                           total_samples, sv_list, block_samples=_BLOCK_SAMPLES)
            bs._out_path_keep = out_p
            bs.out_path = out_p
            keep_alive.append(out_p)
        band_specs.append(bs)
        meta_bands.append({
            "id": plan.id, "centre_hz": plan.centre_hz, "fs": plan.fs,
            "file": plan.out_file, "systems": sorted(band_sys),
            "nsv_ref_scale": round(pnorm, 4),
        })

    if plan_only:
        if not band_plans:
            raise RuntimeError("no satellites survived band planning")
        return EnginePlan(bands=band_plans)
    if not band_specs:
        raise RuntimeError("no satellites survived band planning")
    plans_by_id = {p.id: p for p in plans}

    cb = None
    if progress_cb is not None:
        def cb(frac, _user=None):        # _lib wraps this in a 2-arg CFUNCTYPE
            progress_cb(float(frac))

    rc = _lib.run_bands(band_specs, cb)
    if rc != 0:
        raise RuntimeError(f"synth_run_bands failed ({rc})")

    if in_memory:
        return mem_buffers

    # Composite-signal models, per band file, streamed (see postproc):
    # multipath under models_to_iq like the gps-sdr-sim path, then the RF
    # impairments (random_seed overrides their seed, as there).
    channel_report = None
    if to_iq and (rc_cfg.enabled or chan.multipath.enabled):
        channel_report = {
            "receiver_clock": (rc_mod.state(rc_cfg, sow) | {
                "applied": "per satellite, per band (trajectory knots)"})
            if rc_cfg.enabled else None,
            "multipath": None,
        }
    imp_cfg = imp_mod.ImpairmentConfig.from_dict(getattr(req, "impairments", None))
    if getattr(req, "random_seed", None) is not None and req.impairments:
        imp_cfg = dataclasses.replace(imp_cfg, seed=req.random_seed)
    impairment_report = {}
    for bi, mb in enumerate(meta_bands):
        path = outdir / mb["file"]
        if to_iq and chan.multipath.enabled:
            r = postproc.apply_multipath(path, mb["fs"], plans_by_id[mb["id"]].quant,
                                         chan.multipath)
            r["tracking_bias"] = mp_mod.tracking_bias(chan.multipath, 0.0)
            channel_report["multipath"] = (channel_report["multipath"] or {}) | {
                mb["id"]: r}
        if imp_cfg.enabled:
            impairment_report[mb["id"]] = postproc.apply_impairments(
                path, mb["fs"], plans_by_id[mb["id"]].quant, imp_cfg, band_idx=bi)

    l1_plan = next((p for p in plans if p.id == "L1"), plans[0])
    l1_total = int(round(l1_plan.fs * req.duration_s))
    meta = {
        "sample_rate": l1_plan.fs,
        "sample_format": req.sample_format,
        "total_samples": l1_total,
        "created_utc": created.isoformat(),
        "output": l1_plan.out_file,
        "config": {
            "lat": req.lat, "lon": req.lon, "alt": req.alt,
            "start_utc": req.start.isoformat(), "duration_s": req.duration_s,
            "rinex_path": req.rinex_path, "ephemeris_mode": ephemeris_mode,
        },
        "provenance": {
            "engine": "native",
            "ephemeris": ephemeris_mode,
            "prns": [e["prn"] for e in entries if e["sys"] == "G"],
            "trajectory": ("per-block re-propagation"
                           if getattr(req, "continuous_doppler", True)
                           else "constant Doppler at run start"),
            "phase1_approx": ("per-block re-propagation"
                              if getattr(req, "continuous_doppler", True)
                              else "observables at run start epoch, "
                                   "Doppler held constant over the run"),
            "route": ({"waypoints": len(route), "mode": "linear-interp"}
                      if route else "static"),
            "nav": nav_prov,
            "signals": sorted({
                {v: k for k, v in signals.SIGNALS.items()}.get(
                    e["signal_id"], f"{e['sys']}/{e['signal_id'].band}")
                for e in entries}),
            # speed_mps None: the channel follows `motion` (the route's
            # distance profile) when present, else a static receiver.
            "fading": ({"model": cfg.model, "environment": cfg.environment,
                        "speed_mps": cfg.speed_mps,
                        "motion": (fade_motion.as_dict()
                                   if fade_motion is not None else None)}
                       if cfg.enabled() else "off"),
            "channel_models": channel_report,
            "impairments": impairment_report or None,
            "svs": meta_svs,
            "systems": sorted({e["sys"] for e in entries}),
            "warnings": warnings,
        },
        "bands": meta_bands,
    }
    if not getattr(req, "skip_meta", False):
        (outdir / "meta.json").write_text(json.dumps(meta, indent=2))
    return outdir
