from __future__ import annotations

import ctypes
import datetime as dt
import json
import logging
import math
import pathlib

from backend import config, ephemeris, ephemeris_source, geometry
from backend.synth import _lib, bands, signals
from backend.synth.fading import FadingConfig

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

# Constellation int -- matches the synth_code CONSTELLATION dispatch. This is the
# key the mixer/fading use; it is NOT the same as the code_sys passed to
# _lib.code (which selects a code VARIANT). For Galileo they intentionally
# differ: spec.sys == 5 (Galileo) while code_sys == 6 (E1C pilot).
_SYS_INT = {"G": 0, "J": 1, "S": 2, "C": 3, "R": 4, "E": 5}
_E1_IS_PILOT = True     # E1C pilot for acquisition; E1B when nav bits added

# RINEX per-system satellite numbers -> native synth_code PRN domain.
#   QZSS  Jnn  -> PRN 192+nn  (J01 = 193, native range 193..202)
#   SBAS  Snn  -> PRN 100+nn  (S20 = 120, native range 120..158)
# G/E/C already agree with the native domain; R (GLONASS) is FDMA so its code
# is common to all slots and the number is irrelevant to code generation.
_PRN_OFFSET = {"J": 192, "S": 100}


def _native_prn(sysc: str, prn: int) -> int:
    return prn + _PRN_OFFSET.get(sysc, 0)


_GLO_G1_CODE = None


def _glo_g1_code():
    """GLONASS G1 C/A ranging code: a single 511-chip m-sequence (common to
    every slot -- GLONASS is FDMA, not CDMA). 9-stage LFSR, polynomial
    1 + x^5 + x^9, all-ones seed, output tapped at stage 7. Returned as an
    int8 {-1,+1} array. Cached. (Generated in Python: native ``synth_code``
    has no GLONASS branch and rejects prim_len < 1023.)"""
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


def _sv_spec_for(entry, gain):
    """Turn one ``constellation_multi`` entry into a ready ``_lib.SvSpec`` plus
    the ctypes buffers that must outlive it (``keep``). The full multi-system
    ``run()`` wiring lands in Task 16/17; here it is unit-tested directly."""
    sig = entry["signal_id"]
    sysc = entry["sys"]
    if sysc == "R":
        k = entry.get("glo_k")
        if k is None or (isinstance(k, float) and math.isnan(k)):
            return None, f"GLONASS PRN {entry.get('prn')}: missing glo_k, skipped"
    prim_len = sig.code_len
    if sysc == "E":
        code_sys = 6 if _E1_IS_PILOT else 5   # code VARIANT, not constellation
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
    spec = _lib.SvSpec()
    pbuf = (ctypes.c_int8 * prim_len)(*prim.tolist())
    spec.code = pbuf
    keep = [pbuf]
    spec.code_len = sig.code_len
    spec.chip_rate_hz = sig.chip_rate_hz
    spec.carrier_freq_hz = entry["carrier_doppler_hz"]
    if sysc == "R":
        # FDMA: the G1 recorder LO sits at 1602.0 MHz; this SV's carrier is
        # offset by k * 562.5 kHz plus its geometric Doppler.
        spec.carrier_freq_hz += signals.glo_channel_offset_hz(int(entry["glo_k"]))
    spec.carrier_phase0_rad = 0.0
    spec.code_phase0_chips = entry["code_phase_chips"]
    spec.code_doppler_hz = entry["code_doppler_hz"]
    spec.nav_mode = 0
    spec.nav_bits = None
    spec.nav_nbits = 0
    spec.gain = gain
    spec.prn = entry["prn"]
    spec.sys = _SYS_INT[sysc]
    spec.sub_carrier_hz = sig.sub_carrier_hz
    if sec_len and sec is not None:
        sbuf = (ctypes.c_int8 * sec_len)(*sec.tolist())
        spec.sec_code = sbuf
        spec.sec_len = sec_len
        # B1I NH20: 20 chips, one per 1 ms primary period -> 1000 Hz.
        # E1C CS25: 25 chips, one per 4 ms E1C primary code -> 250 Hz.
        spec.sec_rate_hz = 250.0 if sysc == "E" else 1000.0
        keep.append(sbuf)
    else:
        spec.sec_code = None
        spec.sec_len = 0
        spec.sec_rate_hz = 0.0
    # spec.fading left zeroed: SvSpec() zero-inits and FadingCfg model 0 = off.
    return spec, keep


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


def run(req, progress_cb=None) -> pathlib.Path:
    """Synthesize a multi-GNSS run with the native engine. Produces one IQ file
    per RF band (``gpssim.bin`` for L1, ``gpssim_g1.bin`` for the GLONASS G1
    FDMA band) plus ``meta.json``. Returns the output directory.

    GPS-only (``req.systems == ("G",)``) is byte-identical to the Phase-1
    single-band path: one L1 band, one ``gpssim.bin``.
    """
    _lib.load_lib()

    created = dt.datetime.now(dt.timezone.utc)
    outdir = config.OUT_DIR / created.strftime("%Y%m%dT%H%M%S%f")
    outdir.mkdir(parents=True, exist_ok=True)

    systems = tuple(getattr(req, "systems", None) or ("G",))
    warnings: list[str] = []

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
        r_keys = [k for k in state_fns if k[0] == "R"]
        if r_keys:
            r_eph: dict = {}
            try:
                r_eph = ephemeris.parse_rinex_multi(req.rinex_path, ("R",),
                                                    require=())
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

        stubs: dict = {}
        for k in state_fns:
            rec = {"system": k[0]}
            if k[0] == "R":
                rec["glo_k"] = glo_k_by_key[k]
            stubs[k] = rec

        rx = geometry.llh_to_ecef(req.lat, req.lon, req.alt)
        entries = geometry.constellation_multi(
            stubs, rx, p_sow, signals.signal_for, mask_deg=5.0,
            state_fn_by_key=state_fns)
        entries.sort(key=lambda e: (e["sys"], e["prn"]))
        for e in entries:
            e["prn"] = _native_prn(e["sys"], e["prn"])
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
            eph = ephemeris.parse_rinex_multi(req.rinex_path, systems,
                                              require=("G",))
            got = {(k[0] if isinstance(k, tuple) else "G") for k in eph}
            dropped = [s for s in systems if s not in got]
            if dropped:
                warnings.append(f"RINEX has no records for systems {dropped!r}; "
                                "generated the remaining systems")
                systems = tuple(s for s in systems if s in got)

        eph = ephemeris.align_epochs(eph, week, sow)
        rx = geometry.llh_to_ecef(req.lat, req.lon, req.alt)
        entries = geometry.constellation_multi(eph, rx, sow, signals.signal_for,
                                               mask_deg=5.0)
        entries.sort(key=lambda e: (e["sys"], e["prn"]))
        for e in entries:
            e["prn"] = _native_prn(e["sys"], e["prn"])

    cfg = FadingConfig.from_dict(getattr(req, "fading", None))
    fading_model_int = 1 if cfg.model == "lognormal" else 0

    plans = bands.plan_bands(entries, req)
    if not plans:
        raise RuntimeError("no visible satellites for any band")

    band_specs = []
    keep_alive = []
    meta_bands = []
    meta_svs = []
    for plan in plans:
        sv_list = []
        band_sys = set()
        for e in plan.entries:
            el_deg = float(e.get("el_deg", 90.0))
            static_gain = _el_gain(el_deg)
            spec, keep = _sv_spec_for(e, static_gain)
            if spec is None:
                _log.warning("engine.run: %s", keep)
                warnings.append(str(keep))
                continue
            spec.fading.model = fading_model_int
            spec.fading.sigma_db = cfg.sigma_db
            spec.fading.coherence_s = cfg.coherence_s
            spec.fading.seed = cfg.seed
            sv_list.append(spec)
            keep_alive.append(keep)
            band_sys.add(e["sys"])
            sig = e["signal_id"]
            sv_meta = {"sys": e["sys"], "prn": e["prn"],
                       "code_len": sig.code_len,
                       "chip_hz": sig.chip_rate_hz,
                       "code_doppler_hz": e["code_doppler_hz"],
                       "el_deg": round(el_deg, 2),
                       "az_deg": round(float(e.get("az_deg", 0.0)), 2),
                       # static elevation amplitude taper applied to this SV;
                       # per-block lognormal fading (sigma_db below) rides on
                       # top of it in the C++ mixer, so the realised power is
                       # time-varying around this level.
                       "gain": round(static_gain, 4),
                       "gain_db": round(20.0 * math.log10(static_gain), 2),
                       "fading_sigma_db": cfg.sigma_db if fading_model_int else 0.0}
            gk = e.get("glo_k")
            if e["sys"] == "R" and gk is not None and not (
                    isinstance(gk, float) and math.isnan(gk)):
                sv_meta["glo_k"] = int(gk)
            meta_svs.append(sv_meta)
        if not sv_list:
            continue
        total_samples = int(round(plan.fs * req.duration_s))
        bs = _lib.BandSpec()
        _lib.fill_band(bs, outdir / plan.out_file, plan.fs, plan.quant,
                       total_samples, sv_list)
        band_specs.append(bs)
        meta_bands.append({
            "id": plan.id, "centre_hz": plan.centre_hz, "fs": plan.fs,
            "file": plan.out_file, "systems": sorted(band_sys),
        })

    if not band_specs:
        raise RuntimeError("no satellites survived band planning")

    cb = None
    if progress_cb is not None:
        def cb(frac, _user=None):        # _lib wraps this in a 2-arg CFUNCTYPE
            progress_cb(float(frac))

    rc = _lib.run_bands(band_specs, cb)
    if rc != 0:
        raise RuntimeError(f"synth_run_bands failed ({rc})")

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
            "phase1_approx": "observables at run start epoch, Doppler held constant over the run",
            "fading": cfg.model,
            "svs": meta_svs,
            "systems": sorted({e["sys"] for e in entries}),
            "warnings": warnings,
        },
        "bands": meta_bands,
    }
    (outdir / "meta.json").write_text(json.dumps(meta, indent=2))
    return outdir
