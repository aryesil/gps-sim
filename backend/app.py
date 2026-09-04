# backend/app.py
from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import shutil
import threading

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend import (config, ephemeris, geometry, scenario, generator,
                     inspector, receiver, lnav_display, transmit)

app = FastAPI(title="GPS L1 C/A Signal Simulator")


@app.exception_handler(ephemeris.EphemerisUnavailable)
def _eph_unavailable(request: Request, exc: ephemeris.EphemerisUnavailable):
    return JSONResponse(
        status_code=503,
        content={"detail": f"ephemeris unavailable: {exc}. "
                           "Put a local RINEX path in the RINEX field instead of AUTO."})
_FRONT = pathlib.Path(__file__).resolve().parent.parent / "frontend"
_tx_lock = threading.Lock()
_tx_stop = threading.Event()


def download_free_bytes(path) -> int:
    return shutil.disk_usage(path).free


def _finite(x):
    return x if isinstance(x, (int, float)) and math.isfinite(x) else None


def _rinex_dir():
    return config.DATA_DIR / "rinex"


def _newest_cached_rinex():
    d = _rinex_dir()
    if not d.is_dir():
        return None
    files = [p for p in d.iterdir()
             if p.is_file() and (p.suffix == ".rnx" or p.name[-1:].lower() == "n")]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def _resolve_eph(date, rinex_path=None):
    """Return (eph_by_prn, source_label).

    - explicit real path         -> parse that file
    - 'AUTO'/None                -> that day's cache, else download,
                                    else fall back to the newest cached RINEX
    """
    if rinex_path and rinex_path != "AUTO":
        return ephemeris.parse_rinex(rinex_path), pathlib.Path(rinex_path).name
    try:
        return ephemeris.get_ephemeris(date), f"downloaded/cached {date:%Y-%j}"
    except ephemeris.EphemerisUnavailable:
        fb = _newest_cached_rinex()
        if fb is None:
            raise
        return ephemeris.parse_rinex(fb), f"fallback: {fb.name}"


def _has_libiio() -> bool:
    try:
        import iio  # noqa: F401
        return True
    except Exception:
        return False


@app.get("/api/health")
def health():
    return {
        "gps_sdr_sim": pathlib.Path(config.GPS_SDR_SIM_BIN).exists(),
        "georinex": _try_import("georinex"),
        "libiio": _has_libiio(),
        "allow_tx": config.ALLOW_TX,
    }


def _try_import(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


@app.post("/api/preview")
def preview(body: dict):
    start = dt.datetime.fromisoformat(body["start_utc"])
    date = start.date()
    tow = _gps_tow(start)
    eph, eph_src = _resolve_eph(date, body.get("rinex_path"))
    rx = geometry.llh_to_ecef(body["lat"], body["lon"], body["alt"])
    sats = geometry.constellation(eph, rx, tow, body.get("mask_deg", 5.0))
    d = geometry.dop(sats, rx)
    pdop_raw = d["pdop"]
    d = {k: _finite(v) for k, v in d.items()}
    warnings = [f"ephemeris: {eph_src}"]
    if len(sats) < 4:
        warnings.append("fewer than 4 visible satellites — no hardware fix")
    if math.isfinite(pdop_raw) and pdop_raw > 10:
        warnings.append(f"high PDOP {pdop_raw:.1f}")
    for s in sats:
        toe = eph[s["prn"]]["toe"]
        if abs(((tow - toe + 302400) % 604800) - 302400) > 7200:
            warnings.append("start time outside toe +/- 2 h for some satellites")
            break
    return {"satellites": sats, "dop": d, "warnings": warnings}


@app.post("/api/rinex/upload")
async def rinex_upload(date: str, file: UploadFile):
    d = dt.date.fromisoformat(date)
    ephemeris.save_uploaded_rinex(d, await file.read())
    return {"cached": True, "date": date}


@app.post("/api/generate")
def generate(body: dict):
    start = dt.datetime.fromisoformat(body["start_utc"])
    rinex_path = body["rinex_path"]
    if rinex_path == "AUTO":
        try:
            rinex_path = str(ephemeris.cached_rinex_path(start.date()))
        except ephemeris.EphemerisUnavailable:
            fb = _newest_cached_rinex()
            if fb is None:
                raise
            rinex_path = str(fb)
    req = scenario.ScenarioRequest(
        rinex_path=rinex_path, lat=body["lat"], lon=body["lon"], alt=body["alt"],
        start=start, duration_s=int(body["duration_s"]),
        sample_rate=float(body.get("sample_rate", config.DEFAULT_SAMPLE_RATE)),
        sample_format=body.get("sample_format", "int16"),
        route=[tuple(p) for p in body["route"]] if body.get("route") else None,
    )
    if scenario.estimate_bytes(req) > download_free_bytes(config.OUT_DIR):
        raise HTTPException(507, "estimated IQ size exceeds free disk space")

    def events():
        try:
            q: list = []
            outdir = generator.run(req, progress_cb=lambda f: q.append(f))
            for f in q:
                yield f"data: {json.dumps({'progress': f})}\n\n"
            # generator.run aligns every satellite's toc/toe to the request's
            # start (KNOWN_ISSUES F4) before handing the nav file to
            # gps-sdr-sim, so the IQ was generated from that aligned
            # ephemeris, not the original broadcast one. Build "expected"
            # from the same aligned copy here or this comparison reports
            # huge, misleading errors whenever the real epoch was far from
            # the requested start.
            eph, _ = _resolve_eph(req.start.date(), req.rinex_path)
            gps_start = req.start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
            week, sow = ephemeris.gps_week_and_sow(gps_start)
            eph = ephemeris.align_epochs(eph, week, sow)
            rx = geometry.llh_to_ecef(req.lat, req.lon, req.alt)
            sats = geometry.constellation(eph, rx, _gps_tow(req.start))
            iq = inspector.read_iq(outdir / "gpssim.bin", req.sample_format,
                                   max_samples=int(req.sample_rate * 0.010))
            table = inspector.compare(iq, req.sample_rate, sats)
        except Exception as e:
            # A mid-stream exception here (e.g. gps-sdr-sim exiting non-zero)
            # would otherwise just kill the SSE connection with no payload --
            # the frontend's reader never sees a final chunk and the progress
            # bar is stuck forever. Always send a terminal event instead.
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            return
        done = {"done": {"outdir": outdir.name,
                         "size_bytes": (outdir / "gpssim.bin").stat().st_size,
                         "inspect": table}}
        yield f"data: {json.dumps(done)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/receiver")
def run_receiver(body: dict):
    outdir = config.OUT_DIR / body["outdir"]
    meta = json.loads((outdir / "meta.json").read_text())
    start = dt.datetime.fromisoformat(meta["config"]["start_utc"])
    # generator.run aligns every satellite's toc/toe to the request's start
    # (KNOWN_ISSUES F4) before generating; the fix solve must use the same
    # aligned ephemeris the transmitted IQ was actually built from, or the
    # receiver's expected geometry mismatches the signal (same bug as the
    # /api/generate inspect step, fixed there first).
    eph, _ = _resolve_eph(start.date(), meta["config"].get("rinex_path"))
    gps_start = start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    week, sow = ephemeris.gps_week_and_sow(gps_start)
    eph = ephemeris.align_epochs(eph, week, sow)
    return receiver.fix_from_iq(
        outdir / "gpssim.bin", meta["sample_format"], meta["sample_rate"],
        eph, _gps_tow(start), marker_llh=body.get("marker"))


@app.get("/api/lnav")
def lnav(prn: int, outdir: str):
    od = config.OUT_DIR / outdir
    meta = json.loads((od / "meta.json").read_text())
    start = dt.datetime.fromisoformat(meta["config"]["start_utc"])
    # Same alignment as generator.run/inspect/receiver (KNOWN_ISSUES F4): the
    # generated IQ's subframes actually carry the aligned toc/toe/week, not
    # the original broadcast ones.
    eph_all, _ = _resolve_eph(start.date(), meta["config"].get("rinex_path"))
    gps_start = start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    week, sow = ephemeris.gps_week_and_sow(gps_start)
    eph = ephemeris.align_epochs(eph_all, week, sow)[prn]
    return lnav_display.explain(eph, tow_count=int(_gps_tow(start) / 6), week=int(eph.get("gps_week", 0)))


@app.post("/api/transmit")
def start_transmit(body: dict):
    if not config.ALLOW_TX or not body.get("confirm_isolated"):
        raise HTTPException(403, "transmit disabled: needs ALLOW_TX and confirm_isolated")
    if _tx_lock.locked():
        raise HTTPException(409, "a transmit is already running")
    params = transmit.TxParams(
        iq_path=str(config.OUT_DIR / body["outdir"] / "gpssim.bin")
        if "outdir" in body else body["iq_path"],
        sample_rate=float(body["sample_rate"]), sample_format=body["sample_format"],
        lo_hz=float(body.get("lo_hz", config.L1_HZ)),
        tx_gain_db=float(body.get("tx_gain_db", -50.0)),
        uri=body.get("uri", config.DEVICE_URI),
        tx_scale=float(body.get("tx_scale", 1.0)))
    _tx_stop.clear()

    def events():
        with _tx_lock:
            q: list = []
            def cb(d):
                d["fraction"] = None
                q.append(d)
            th = threading.Thread(target=transmit.stream,
                                  kwargs=dict(params=params, dry_run=body.get("dry_run", False),
                                              progress_cb=cb, cancel=_tx_stop))
            th.start()
            while th.is_alive() or q:
                while q:
                    yield f"data: {json.dumps(q.pop(0))}\n\n"
                if _tx_stop.is_set():
                    break
                th.join(timeout=0.2)
            yield f"data: {json.dumps({'finished': True})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/transmit/stop")
def stop_transmit():
    _tx_stop.set()
    return {"stopped": True}


def _gps_tow(when_utc: dt.datetime) -> float:
    epoch = dt.datetime(1980, 1, 6)
    delta = when_utc - epoch
    tow = (delta.days % 7) * 86400 + delta.seconds + delta.microseconds / 1e6
    tow += config.GPS_UTC_LEAP_S
    return tow % (7 * 86400)


@app.get("/")
def index():
    return FileResponse(_FRONT / "index.html")


config.OUT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/out", StaticFiles(directory=str(config.OUT_DIR)), name="out")
app.mount("/static", StaticFiles(directory=str(_FRONT)), name="static")
