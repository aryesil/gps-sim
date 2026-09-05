from __future__ import annotations

import ctypes
import datetime as dt
import json
import pathlib

from backend import config, ephemeris, geometry
from backend.synth import _lib
from backend.synth._lib import RunSpec, SvSpec, _bind_run

_KEPLER_KEYS = ("sqrtA e m0 delta_n omega omega0 omega_dot i0 idot cuc cus crc "
                "crs cic cis toe toc af0 af1 af2").split()


def kepler_struct(eph: dict) -> "_lib.KeplerEph":
    s = _lib.KeplerEph()
    for k in _KEPLER_KEYS:
        setattr(s, k, float(eph[k]))
    s._pad = 0.0
    return s


_QUANT = {"int8": 0, "int12": 1, "int16": 2}


def _visible_gps(req) -> list[tuple[int, dict]]:
    """Visible GPS PRNs (el_deg >= 5) with observables evaluated once at the
    run mid-epoch -- the Phase 1 constant-Doppler approximation."""
    eph = ephemeris.parse_rinex(req.rinex_path)
    gps_start = req.start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    week, sow = ephemeris.gps_week_and_sow(gps_start)
    eph = ephemeris.align_epochs(eph, week, sow)
    rx = geometry.llh_to_ecef(req.lat, req.lon, req.alt)
    t_mid = sow + req.duration_s / 2.0
    out = []
    for prn in sorted(eph):
        o = geometry.observables(eph[prn], rx, t_mid)
        if o["el_deg"] >= 5.0:
            out.append((prn, o))
    return out


def run(req, progress_cb=None) -> pathlib.Path:
    """Synthesize a full GPS L1 C/A run with the native engine. Returns the
    output directory holding ``gpssim.bin`` and ``meta.json``."""
    lib = _lib.load_lib()
    _bind_run(lib)

    created = dt.datetime.now(dt.timezone.utc)
    outdir = config.OUT_DIR / created.strftime("%Y%m%dT%H%M%S%f")
    outdir.mkdir(parents=True, exist_ok=True)
    out_bin = outdir / "gpssim.bin"

    sats = _visible_gps(req)
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

    rs = RunSpec()
    rs.fs = float(req.sample_rate)
    rs.quant = _QUANT[req.sample_format]
    rs.dither = 0
    rs.total_samples = int(round(req.sample_rate * req.duration_s))
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
        "sample_rate": req.sample_rate,
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
            "phase1_approx": "constant Doppler at run mid-point",
        },
    }
    (outdir / "meta.json").write_text(json.dumps(meta, indent=2))
    return outdir
