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
                     inspector, receiver, lnav_display, transmit, live, trajectory)

app = FastAPI(title="GPS L1 C/A Signal Simulator")


@app.exception_handler(ephemeris.EphemerisUnavailable)
def _eph_unavailable(request: Request, exc: ephemeris.EphemerisUnavailable):
    return JSONResponse(
        status_code=503,
        content={"detail": f"ephemeris unavailable: {exc}. "
                           "Put a local RINEX path in the RINEX field instead of AUTO."})
_FRONT = pathlib.Path(__file__).resolve().parent.parent / "frontend"
# TX1/TX2 -- the PlutoSDR's two real simultaneous outputs (KNOWN hardware
# fact, not an arbitrary cap). Each slot holds {"stop": Event, "session":
# LiveSession | None} for whatever is currently occupying it, or None.
_tx_slots: dict[str, dict | None] = {"TX1": None, "TX2": None}
_tx_slots_lock = threading.Lock()


def _acquire_tx_slot() -> str:
    with _tx_slots_lock:
        for slot, occ in _tx_slots.items():
            if occ is None:
                _tx_slots[slot] = {"stop": threading.Event(), "session": None}
                return slot
    raise HTTPException(409, "both TX1 and TX2 are already transmitting")


def _release_tx_slot(slot: str) -> None:
    with _tx_slots_lock:
        _tx_slots[slot] = None


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


@app.post("/api/preview_track")
def preview_track(body: dict):
    # Trajectory playback: same geometry as /api/preview but sampled across
    # the whole scenario duration so the frontend can scrub through how az/el
    # change over time at a fixed receiver point (route interpolation is not
    # replayed here -- this is the satellite side of the picture, not the
    # receiver's path).
    start = dt.datetime.fromisoformat(body["start_utc"])
    eph, eph_src = _resolve_eph(start.date(), body.get("rinex_path"))
    rx = geometry.llh_to_ecef(body["lat"], body["lon"], body["alt"])
    duration_s = int(body.get("duration_s", 300))
    step_s = max(1, int(body.get("step_s", 30)))
    frames = []
    for t_off in range(0, duration_s + 1, step_s):
        tow = _gps_tow(start + dt.timedelta(seconds=t_off))
        sats = geometry.constellation(eph, rx, tow, body.get("mask_deg", 5.0))
        frames.append({"t_offset_s": t_off, "satellites": sats})
    return {"frames": frames, "warnings": [f"ephemeris: {eph_src}"]}


@app.post("/api/rinex/upload")
async def rinex_upload(date: str, file: UploadFile):
    d = dt.date.fromisoformat(date)
    ephemeris.save_uploaded_rinex(d, await file.read())
    return {"cached": True, "date": date}


def _resolve_rinex(body: dict, start: dt.datetime) -> str:
    """Turn a request body's rinex_path into a real file path.

    'AUTO' (the UI's default) means "that day's cached/downloaded RINEX, or
    the newest cached file as a fallback". Anything else is passed through
    as an explicit path. Shared by /api/generate and /api/live/start -- both
    build a ScenarioRequest, which needs a path gps-sdr-sim can open.
    """
    rinex_path = body.get("rinex_path") or "AUTO"
    if rinex_path != "AUTO":
        return rinex_path
    try:
        return str(ephemeris.cached_rinex_path(start.date()))
    except ephemeris.EphemerisUnavailable:
        fb = _newest_cached_rinex()
        if fb is None:
            raise
        return str(fb)


@app.post("/api/generate")
def generate(body: dict):
    start = dt.datetime.fromisoformat(body["start_utc"])
    rinex_path = _resolve_rinex(body, start)
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


@app.get("/api/iqplot")
def iqplot(outdir: str, n: int = 2000):
    od = config.OUT_DIR / outdir
    meta = json.loads((od / "meta.json").read_text())
    # spectrum() needs a full 4096-sample window (its default nfft) even when
    # the caller only wants a shorter waveform/constellation preview, or the
    # PSD gets zero-padded (widened main lobe, misleading plot) whenever n<4096.
    iq = inspector.read_iq(od / "gpssim.bin", meta["sample_format"],
                           max_samples=max(n, 4096))
    freqs, power_db = inspector.spectrum(iq, meta["sample_rate"])
    return {
        "i": iq.real[:n].tolist(), "q": iq.imag[:n].tolist(),
        "spectrum_freq_hz": freqs.tolist(), "spectrum_db": power_db.tolist(),
    }


@app.get("/api/correlation")
def correlation(outdir: str, prn: int):
    od = config.OUT_DIR / outdir
    meta = json.loads((od / "meta.json").read_text())
    iq = inspector.read_iq(od / "gpssim.bin", meta["sample_format"],
                           max_samples=int(meta["sample_rate"] * 0.010))
    res = inspector.acquire(iq, meta["sample_rate"], prn)
    chips, amp = inspector.correlation_curve(iq, meta["sample_rate"], prn, res["doppler_hz"])
    # argsort so the plotted line runs monotonically 0->1023 chips instead of
    # in FFT-bin order (chip = bin_index * chip_rate / sample_rate wraps).
    pairs = sorted(zip(chips.tolist(), amp.tolist()))
    return {
        "prn": prn, "doppler_hz": res["doppler_hz"], "metric_db": res["metric_db"],
        "code_phase_chips": [p[0] for p in pairs], "amplitude": [p[1] for p in pairs],
    }


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
    slot = _acquire_tx_slot()
    try:
        params = transmit.TxParams(
            iq_path=str(config.OUT_DIR / body["outdir"] / "gpssim.bin")
            if "outdir" in body else body["iq_path"],
            sample_rate=float(body["sample_rate"]), sample_format=body["sample_format"],
            lo_hz=float(body.get("lo_hz", config.L1_HZ)),
            tx_gain_db=float(body.get("tx_gain_db", -50.0)),
            uri=body.get("uri", config.DEVICE_URI),
            tx_scale=float(body.get("tx_scale", 1.0)))
        itemsize = 1 if params.sample_format == "int8" else 2
        try:
            total_samples = pathlib.Path(params.iq_path).stat().st_size // (2 * itemsize)
        except OSError:
            total_samples = 0
    except Exception:
        _release_tx_slot(slot)
        raise

    def events():
        try:
            q: list = []
            def cb(d):
                d["fraction"] = (d["samples"] / total_samples) if total_samples else None
                q.append(d)
            th = threading.Thread(target=transmit.stream,
                                  kwargs=dict(params=params, dry_run=body.get("dry_run", False),
                                              progress_cb=cb, cancel=_tx_slots[slot]["stop"]))
            th.start()
            while th.is_alive() or q:
                while q:
                    yield f"data: {json.dumps({**q.pop(0), 'slot': slot})}\n\n"
                if _tx_slots[slot]["stop"].is_set():
                    break
                th.join(timeout=0.2)
            th.join(timeout=2.0)
            yield f"data: {json.dumps({'finished': True, 'slot': slot})}\n\n"
        finally:
            _release_tx_slot(slot)

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/transmit/stop")
def stop_transmit(body: dict | None = None):
    slot = (body or {}).get("slot")
    if slot is None:
        occupied = [s for s, occ in _tx_slots.items() if occ is not None]
        if len(occupied) == 1:
            slot = occupied[0]
        elif len(occupied) == 0:
            return {"stopped": True}
        else:
            raise HTTPException(400, "both TX1 and TX2 occupied: specify {\"slot\": \"TX1\"|\"TX2\"}")
    occ = _tx_slots.get(slot)
    if occ:
        occ["stop"].set()
    return {"stopped": True}


def _tee_spectrogram(chunks, sample_rate: float, on_row, track_prn: int | None = None,
                      on_cn0=None):
    """Wrap a live-session chunk source: forward every IQ chunk unchanged
    to the caller (transmit.stream's hardware push) while also handing
    each chunk's spectrum snapshot to on_row -- one FFT column per
    ~1s segment, computed with the existing inspector.spectrum() (no new
    DSP, same 4096-sample Hanning-windowed FFT /api/iqplot already uses,
    just cheaper: nfft=256 is enough resolution for a live waterfall).

    When track_prn is set, also runs the existing inspector.acquire() on
    each chunk and reports its metric_db to on_cn0 -- one C/N0 trend
    sample per segment, same acquisition already used by /api/correlation
    and the post-generate inspect table, just re-run every ~1s live."""
    for chunk in chunks:
        freqs, db = inspector.spectrum(chunk, sample_rate, nfft=256)
        on_row(freqs, db)
        if track_prn is not None and on_cn0 is not None:
            try:
                res = inspector.acquire(chunk, sample_rate, track_prn)
                on_cn0(res["metric_db"])
            except Exception:
                pass  # a transient acquisition failure must never drop the TX chunk
        yield chunk


@app.post("/api/live/start")
def live_start(body: dict):
    if not config.ALLOW_TX or not body.get("confirm_isolated"):
        raise HTTPException(403, "transmit disabled: needs ALLOW_TX and confirm_isolated")
    slot = _acquire_tx_slot()
    try:
        start = dt.datetime.fromisoformat(body["start_utc"])
        req = scenario.ScenarioRequest(
            rinex_path=_resolve_rinex(body, start), lat=body["lat"], lon=body["lon"], alt=body["alt"],
            start=start, duration_s=int(body.get("duration_s", 300)),
            sample_rate=float(body.get("sample_rate", config.DEFAULT_SAMPLE_RATE)),
            sample_format=body.get("sample_format", "int16"))
        session = live.LiveSession(req)
        _tx_slots[slot]["session"] = session
        params = transmit.TxParams(
            iq_path="(live)", sample_rate=req.sample_rate, sample_format=req.sample_format,
            lo_hz=float(body.get("lo_hz", config.L1_HZ)),
            tx_gain_db=float(body.get("tx_gain_db", -50.0)),
            uri=body.get("uri", config.DEVICE_URI))
    except Exception:
        _release_tx_slot(slot)
        raise

    def events():
        try:
            q: list = []
            def cb(d):
                d["fraction"] = None  # unbounded live stream -- no total to divide by
                q.append(d)
            def on_row(freqs, db):
                q.append({"spectrogram_freq_hz": freqs.tolist(),
                          "spectrogram_db": db.tolist()})
            def on_cn0(metric_db):
                q.append({"cn0_db": metric_db})
            track_prn = body.get("track_prn")
            chunk_source = _tee_spectrogram(
                session.segments(), req.sample_rate, on_row,
                track_prn=int(track_prn) if track_prn else None, on_cn0=on_cn0)
            th = threading.Thread(
                target=transmit.stream,
                kwargs=dict(params=params, dry_run=body.get("dry_run", False),
                           progress_cb=cb, cancel=_tx_slots[slot]["stop"],
                           chunk_source=chunk_source))
            th.start()
            while th.is_alive() or q:
                while q:
                    yield f"data: {json.dumps({**q.pop(0), 'slot': slot})}\n\n"
                if _tx_slots[slot]["stop"].is_set():
                    break
                th.join(timeout=0.2)
            th.join(timeout=2.0)
            yield f"data: {json.dumps({'finished': True, 'slot': slot})}\n\n"
        finally:
            _release_tx_slot(slot)

    return StreamingResponse(events(), media_type="text/event-stream")


def _session_for_slot(slot: str) -> live.LiveSession:
    occ = _tx_slots.get(slot)
    if not occ or occ.get("session") is None:
        raise HTTPException(404, f"no live session on {slot}")
    return occ["session"]


@app.post("/api/live/jog")
def live_jog(body: dict):
    session = _session_for_slot(body["slot"])
    session.jog(body["direction"], float(body["distance_m"]))
    return {"llh": session.state.llh}


@app.post("/api/live/time_shift")
def live_time_shift(body: dict):
    s = _session_for_slot(body["slot"])
    s.shift_time(body["field"], float(body["delta"]))
    return {"time_offset_s": s.state.time_offset_s}


@app.post("/api/live/stop")
def live_stop(body: dict):
    slot = body["slot"]
    occ = _tx_slots.get(slot)
    if occ:
        occ["stop"].set()
        if occ["session"]:
            occ["session"].stop()
    return {"stopped": True}


@app.post("/api/trajectory/save")
def trajectory_save(body: dict):
    try:
        trajectory.save(body["name"], body["waypoints"])
    except KeyError as e:
        raise HTTPException(400, f"missing field: {e}")
    except trajectory.TrajectoryError as e:
        raise HTTPException(400, str(e))
    return {"saved": body["name"]}


@app.get("/api/trajectory/list")
def trajectory_list():
    return {"names": trajectory.list_names()}


@app.get("/api/trajectory/load")
def trajectory_load(name: str):
    try:
        return {"waypoints": trajectory.load(name)}
    except trajectory.TrajectoryError as e:
        raise HTTPException(404, str(e))


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
