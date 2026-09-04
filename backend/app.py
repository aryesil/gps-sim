# backend/app.py
from __future__ import annotations

import datetime as dt
import json
import pathlib
import shutil
import threading

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend import (config, ephemeris, geometry, scenario, generator,
                     inspector, receiver, lnav_display, transmit)

app = FastAPI(title="GPS L1 C/A Signal Simulator")
_FRONT = pathlib.Path(__file__).resolve().parent.parent / "frontend"
_tx_lock = threading.Lock()
_tx_stop = threading.Event()


def download_free_bytes(path) -> int:
    return shutil.disk_usage(path).free


def _has_libiio() -> bool:
    try:
        import iio  # noqa: F401
        return True
    except ImportError:
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
    except ImportError:
        return False


@app.post("/api/preview")
def preview(body: dict):
    start = dt.datetime.fromisoformat(body["start_utc"])
    date = start.date()
    tow = _gps_tow(start)
    eph = ephemeris.get_ephemeris(date)
    rx = geometry.llh_to_ecef(body["lat"], body["lon"], body["alt"])
    sats = geometry.constellation(eph, rx, tow, body.get("mask_deg", 5.0))
    d = geometry.dop(sats, rx)
    warnings = []
    if len(sats) < 4:
        warnings.append("fewer than 4 visible satellites — no hardware fix")
    if d["pdop"] > 10:
        warnings.append(f"high PDOP {d['pdop']:.1f}")
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
    req = scenario.ScenarioRequest(
        rinex_path=body["rinex_path"], lat=body["lat"], lon=body["lon"], alt=body["alt"],
        start=dt.datetime.fromisoformat(body["start_utc"]), duration_s=int(body["duration_s"]),
        sample_rate=float(body.get("sample_rate", config.DEFAULT_SAMPLE_RATE)),
        sample_format=body.get("sample_format", "int16"),
        route=[tuple(p) for p in body["route"]] if body.get("route") else None,
    )
    if scenario.estimate_bytes(req) > download_free_bytes(config.OUT_DIR):
        raise HTTPException(507, "estimated IQ size exceeds free disk space")

    def events():
        q: list = []
        outdir = generator.run(req, progress_cb=lambda f: q.append(f))
        for f in q:
            yield f"data: {json.dumps({'progress': f})}\n\n"
        eph = ephemeris.get_ephemeris(req.start.date())
        rx = geometry.llh_to_ecef(req.lat, req.lon, req.alt)
        sats = geometry.constellation(eph, rx, _gps_tow(req.start))
        iq = inspector.read_iq(outdir / "gpssim.bin", req.sample_format,
                               max_samples=int(req.sample_rate * 0.010))
        table = inspector.compare(iq, req.sample_rate, sats)
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
    eph = ephemeris.get_ephemeris(start.date())
    return receiver.fix_from_iq(
        outdir / "gpssim.bin", meta["sample_format"], meta["sample_rate"],
        eph, _gps_tow(start), marker_llh=body.get("marker"))


@app.get("/api/lnav")
def lnav(prn: int, outdir: str):
    od = config.OUT_DIR / outdir
    meta = json.loads((od / "meta.json").read_text())
    start = dt.datetime.fromisoformat(meta["config"]["start_utc"])
    eph = ephemeris.get_ephemeris(start.date())[prn]
    return lnav_display.explain(eph, tow_count=int(_gps_tow(start) / 6), week=eph.get("gps_week", 0))


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
        uri=body.get("uri", config.DEVICE_URI))
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


_GPS_UTC_LEAP_S = 18.0  # GPS - UTC, valid 2017-01 .. (update when a leap second is added)


def _gps_tow(when_utc: dt.datetime) -> float:
    epoch = dt.datetime(1980, 1, 6)
    delta = when_utc - epoch
    tow = (delta.days % 7) * 86400 + delta.seconds + delta.microseconds / 1e6
    tow += _GPS_UTC_LEAP_S
    return tow % (7 * 86400)


@app.get("/")
def index():
    return FileResponse(_FRONT / "index.html")


app.mount("/static", StaticFiles(directory=str(_FRONT)), name="static")
