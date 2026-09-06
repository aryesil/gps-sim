from __future__ import annotations

import ctypes
import datetime as dt
import json
import pathlib

from backend import config, ephemeris, geometry
from backend.synth import _lib, fs_policy
from backend.synth._lib import RunSpec, SvSpec
from backend.synth.fading import FadingConfig

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


def _sv_spec_for(entry, gain):
    """Turn one ``constellation_multi`` entry into a ready ``_lib.SvSpec`` plus
    the ctypes buffers that must outlive it (``keep``). The full multi-system
    ``run()`` wiring lands in Task 16/17; here it is unit-tested directly."""
    sig = entry["signal_id"]
    sysc = entry["sys"]
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

    prim, sec = _lib.code(code_sys, entry["prn"], prim_len, sec_len)
    spec = _lib.SvSpec()
    pbuf = (ctypes.c_int8 * prim_len)(*prim.tolist())
    spec.code = pbuf
    keep = [pbuf]
    spec.carrier_freq_hz = entry["carrier_doppler_hz"]
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
    """Synthesize a full GPS L1 C/A run with the native engine. Returns the
    output directory holding ``gpssim.bin`` and ``meta.json``."""
    lib = _lib.load_lib()

    created = dt.datetime.now(dt.timezone.utc)
    outdir = config.OUT_DIR / created.strftime("%Y%m%dT%H%M%S%f")
    outdir.mkdir(parents=True, exist_ok=True)
    out_bin = outdir / "gpssim.bin"

    sats = _visible_gps(req)
    cfg = FadingConfig.from_dict(getattr(req, "fading", None))
    fading_model_int = 1 if cfg.model == "lognormal" else 0
    code_bufs = []                       # keep C-side code pointers alive
    specs = (SvSpec * len(sats))()
    for i, (prn, o) in enumerate(sats):
        cbuf = (ctypes.c_int8 * 1023)()
        if lib.synth_ca_code(prn, cbuf, 1023) != 0:
            raise RuntimeError(f"synth_ca_code failed for PRN {prn}")
        code_bufs.append(cbuf)
        specs[i].code = cbuf
        # Baseband recording: the SV carrier rotates only at the Doppler rate
        # (the L1 centre frequency is the recorder's LO and mixes to 0). This
        # matches Task 7's single-SV kernel and inspector.acquire's search.
        specs[i].carrier_freq_hz = o["carrier_doppler_hz"]
        specs[i].carrier_phase0_rad = 0.0
        specs[i].code_phase0_chips = o["code_phase_chips"]
        specs[i].code_doppler_hz = o["code_doppler_hz"]
        specs[i].nav_mode = 0
        specs[i].nav_bits = None
        specs[i].nav_nbits = 0
        specs[i].gain = 1.0
        specs[i].prn = prn
        specs[i].fading.model = fading_model_int
        specs[i].fading.sigma_db = cfg.sigma_db
        specs[i].fading.coherence_s = cfg.coherence_s
        specs[i].fading.seed = cfg.seed

    fs = fs_policy.validate_fs(req.sample_rate, ["GPS_L1CA"])

    rs = RunSpec()
    rs.fs = fs
    rs.quant = _QUANT[req.sample_format]
    rs.dither = 0
    rs.total_samples = int(round(fs * req.duration_s))
    rs.block_samples = 65536
    rs.nthreads = 0

    cb = None
    if progress_cb is not None:
        @_lib._PROGRESS_CB
        def cb(frac, _user):            # noqa: F811 -- ctypes callback
            progress_cb(float(frac))

    rc = lib.synth_run(str(out_bin).encode(), ctypes.byref(rs), specs,
                       len(sats), cb, None)
    if rc != 0:
        raise RuntimeError(f"synth_run failed ({rc})")

    meta = {
        "sample_rate": fs,
        "sample_format": req.sample_format,
        "total_samples": int(rs.total_samples),
        "created_utc": created.isoformat(),
        "output": "gpssim.bin",
        "config": {
            "lat": req.lat, "lon": req.lon, "alt": req.alt,
            "start_utc": req.start.isoformat(), "duration_s": req.duration_s,
            "rinex_path": req.rinex_path, "ephemeris_mode": "broadcast",
        },
        "provenance": {
            "engine": "native",
            "prns": [p for p, _ in sats],
            "phase1_approx": "observables at run start epoch, Doppler held constant over the run",
            "fading": cfg.model,
        },
    }
    (outdir / "meta.json").write_text(json.dumps(meta, indent=2))
    return outdir
