from __future__ import annotations

import ctypes
import datetime as dt
import json
import logging
import math
import pathlib

from backend import config, ephemeris, geometry
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


_QUANT = {"int8": 0, "int12": 1, "int16": 2}

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
        prim, sec = _lib.code(code_sys, entry["prn"], prim_len, sec_len)
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
            spec, keep = _sv_spec_for(e, _el_gain(e.get("el_deg", 90.0)))
            if spec is None:
                _log.warning("engine.run: %s", keep)
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
                       "chip_hz": sig.chip_rate_hz}
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
            "rinex_path": req.rinex_path, "ephemeris_mode": "broadcast",
        },
        "provenance": {
            "engine": "native",
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
