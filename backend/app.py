# backend/app.py
from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import shutil
import threading
import time

import asyncio
import contextlib

from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend import (config, ephemeris, geometry, scenario, generator,
                     inspector, receiver, lnav_display, transmit, live, trajectory, audit,
                     scenario_lib, recording, receiver_feed, auth, ws_hub, device,
                     precise, ephemeris_source, ephemeris_fit, impairments,
                     channel_models)
from backend.gpstime import GPSTime
from backend.synth import signal_engine, signals

@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    # audit.log_event() runs on worker threads and needs a live asyncio
    # loop to schedule WebSocket sends onto (backend/ws_hub.py).
    ws_hub.set_loop(asyncio.get_event_loop())
    yield


app = FastAPI(title="GPS L1 C/A Signal Simulator", lifespan=_lifespan)


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket):
    """Multi-operator shared view: every audit event (transmit
    start/stop, timeline steps, auto-stop, ...) is pushed here in real
    time to every connected tab -- see backend/ws_hub.py."""
    # Belt-and-suspenders: the lifespan startup hook above sets this too,
    # but a bare TestClient(app) (used by every test in this suite,
    # including this file) never runs lifespan events unless entered as
    # `with TestClient(app) as c:` -- so the first real connection also
    # captures the loop it's actually running on.
    ws_hub.set_loop(asyncio.get_event_loop())
    await ws_hub.register(websocket)
    try:
        while True:
            await websocket.receive_text()  # only used to detect disconnect
    except WebSocketDisconnect:
        pass
    finally:
        ws_hub.unregister(websocket)


@app.exception_handler(ephemeris.EphemerisUnavailable)
def _eph_unavailable(request: Request, exc: ephemeris.EphemerisUnavailable):
    return JSONResponse(
        status_code=503,
        content={"detail": f"ephemeris unavailable: {exc}. "
                           "Put a local RINEX path in the RINEX field instead of AUTO."})


@app.exception_handler(ephemeris_source.EphemerisModeError)
def _eph_mode_error(request: Request, exc: ephemeris_source.EphemerisModeError):
    # Invalid mode, or 'precise' asked for with no usable precise data for
    # the epoch. Never silently served as broadcast (Phase 7).
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(precise.PreciseProductError)
def _precise_error(request: Request, exc: precise.PreciseProductError):
    return JSONResponse(status_code=422, content={"detail": f"precise ephemeris: {exc}"})


# One process-wide precise (SP3) product, loaded on demand via
# /api/precise/load. Analysis only -- it never reaches signal generation.
_precise_provider = precise.PreciseEphemerisProvider()
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


def _preview_multi(body: dict, start: dt.datetime, rx,
                   systems: tuple) -> dict:
    """Geometry preview across multiple constellations (one correlated epoch).
    Same response shape as /api/preview. Channel models / precise mode are
    GPS-only and not applied here."""
    path = _resolve_rinex(body, start)
    gps_start = start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    week, sow = ephemeris.gps_week_and_sow(gps_start)
    try:
        eph = ephemeris.parse_rinex_multi(path, systems, require=("G",))
    except ephemeris.EphemerisUnavailable as e:
        raise HTTPException(422, f"ephemeris: {e}")
    eph = ephemeris.align_epochs(eph, week, sow)
    got = {(k[0] if isinstance(k, tuple) else "G") for k in eph}
    dropped = [s for s in systems if s not in got]

    mask = float(body.get("mask_deg", 5.0))
    ents = geometry.constellation_multi(eph, rx, sow, signals.signal_for,
                                        mask_deg=mask)
    sats = []
    for o in ents:
        s = {k: v for k, v in o.items() if k not in ("signal_id", "_los")}
        s["sys"] = o["sys"]
        s["svid"] = f"{o['sys']}{o['prn']:02d}"
        s["band"] = o["signal_id"].band
        s["_los"] = o["_los"]
        sats.append(s)
    d = {k: _finite(v) for k, v in geometry.dop(ents, rx).items()}
    warnings = [f"ephemeris: {pathlib.Path(path).name}",
                f"multi-GNSS preview: {', '.join(sorted(got))}"]
    if dropped:
        warnings.append(f"no records for {', '.join(dropped)} in this RINEX")
    if ephemeris_source.normalise_mode(body.get("ephemeris_mode")) == "precise":
        warnings.append(
            "multi-GNSS preview uses broadcast ephemeris (precise mode is GPS-only)")
    if channel_models.ChannelModels.from_request(body).any_enabled:
        warnings.append(
            "channel models are not applied to the multi-GNSS preview")
    if len(sats) < 4:
        warnings.append("fewer than 4 visible satellites — no hardware fix")
    return {"satellites": sats, "dop": d, "warnings": warnings,
            "channel_models": None}


@app.post("/api/preview")
def preview(body: dict):
    start = dt.datetime.fromisoformat(body["start_utc"])
    date = start.date()
    tow = _gps_tow(start)
    rx = geometry.llh_to_ecef(body["lat"], body["lon"], body["alt"])

    # Multi-constellation preview: when more than GPS is requested, resolve the
    # mixed nav file and evaluate every system at the one epoch. Channel models
    # and precise mode are GPS-only and stay on the path below.
    req_systems = tuple(dict.fromkeys(body.get("systems") or ["G"]))
    if tuple(sorted(req_systems)) != ("G",):
        return _preview_multi(body, start, rx, req_systems)

    eph, eph_src = _resolve_eph(date, body.get("rinex_path"))

    # Ephemeris mode for the *geometry preview only*. 'precise' uses the
    # loaded SP3 product (analysis); the generated IQ is still broadcast.
    mode = ephemeris_source.normalise_mode(body.get("ephemeris_mode"))
    mode_warnings: list[str] = []
    eph_for_geo = eph
    if mode == "precise":
        auto_warns = _ensure_precise_loaded(
            start, bool(body.get("fallback_to_broadcast")))
        week_t, sow_t = ephemeris.gps_week_and_sow(
            start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S))
        if _precise_provider.loaded:
            eph_for_geo, mode_warnings = ephemeris_source.build_state_fns(
                mode, sorted(eph), GPSTime(week_t, sow_t), eph,
                provider=_precise_provider, on_missing="skip",
                fallback_to_broadcast=bool(body.get("fallback_to_broadcast")))
        mode_warnings = auto_warns + mode_warnings

    try:
        models = channel_models.ChannelModels.from_request(body)
    except ValueError as e:
        raise HTTPException(422, f"channel model: {e}")
    model_args = models.observable_args(tow, body["lat"], body["lon"], body["alt"])
    models_summary = models.summary(tow, body["lat"], body["lon"], body["alt"])

    sats = geometry.constellation(eph_for_geo, rx, tow, body.get("mask_deg", 5.0),
                                  **model_args)
    d = geometry.dop(sats, rx)
    pdop_raw = d["pdop"]
    d = {k: _finite(v) for k, v in d.items()}
    warnings = mode_warnings or [f"ephemeris: {eph_src}"]
    if len(sats) < 4:
        warnings.append("fewer than 4 visible satellites — no hardware fix")
    if math.isfinite(pdop_raw) and pdop_raw > 10:
        warnings.append(f"high PDOP {pdop_raw:.1f}")
    for s in sats:
        toe = eph[s["prn"]]["toe"]
        if abs(((tow - toe + 302400) % 604800) - 302400) > 7200:
            warnings.append("start time outside toe +/- 2 h for some satellites")
            break
    if models.any_enabled:
        warnings.append(
            "channel models applied to the preview observables: "
            + ", ".join(m for m in (
                f"iono={models.atmosphere.ionosphere}" if models.atmosphere.ionosphere != "off" else "",
                f"tropo={models.atmosphere.troposphere}" if models.atmosphere.troposphere != "off" else "",
                f"rx_clock={models.receiver_clock.model}" if models.receiver_clock.enabled else "",
                f"multipath={models.multipath.model}" if models.multipath.enabled else "",
            ) if m))
        if not body.get("models_to_iq"):
            warnings.append(
                "these models shape the preview/truth only — enable "
                "\"apply to IQ\" to also alter the generated signal")
    return {"satellites": sats, "dop": d, "warnings": warnings,
            "channel_models": models_summary}


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


def _ensure_precise_loaded(start: dt.datetime, fallback: bool) -> list[str]:
    """Make ``_precise_provider`` hold an SP3 product that covers ``start``.

    Called on the ``ephemeris_mode == "precise"`` paths. If nothing is
    loaded, or the loaded product does not cover the requested epoch, this
    downloads the best free IGS product for that GPS day
    (``config.PRECISE_SP3_MIRRORS``: rapid tried before final) and loads
    it -- so the operator never has to place a file or click "Fetch".

    Returns advisory warnings. Raises ``ephemeris_source.EphemerisModeError``
    (HTTP 422) when precise data cannot be obtained and ``fallback`` is
    False. Set ``PRECISE_SP3_MIRRORS=""`` to disable the auto-download.
    """
    gps_start = start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    week, sow = ephemeris.gps_week_and_sow(gps_start)
    t_want = GPSTime(week, sow).seconds

    if _precise_provider.loaded:
        lo, hi = _precise_provider.product.coverage_seconds
        # keep clear of the interpolation-window edges (~11-point Lagrange
        # over 15-min epochs); if inside, the loaded product is fine.
        if lo + 1800.0 <= t_want <= hi - 1800.0:
            return []

    if not config.PRECISE_SP3_MIRRORS:
        if fallback:
            return ["precise: no SP3 for this epoch and downloads are disabled; used broadcast"]
        raise ephemeris_source.EphemerisModeError(
            "ephemeris_mode=precise but no SP3 product covers this start time "
            "and PRECISE_SP3_MIRRORS is empty (load one via /api/precise/load)")

    dow = int(sow // 86400)
    # Fetch the day before and after as well and merge: a one-day SP3 file
    # cannot supply a centred ~11-point interpolation window when the start
    # sits within ~3 h of its 00:00 / 24:00 edge (the fit arc is toe +/- 2 h
    # and the window needs several more samples beyond that).
    days = []
    for d in (dow - 1, dow, dow + 1):
        w, dd = week, d
        if dd < 0:
            w, dd = week - 1, 6
        elif dd > 6:
            w, dd = week + 1, 0
        days.append((w, dd))

    products, got, central_ok = [], [], False
    for w, dd in days:
        try:
            p = precise.download_sp3(w, dd, config.PRECISE_DIR,
                                     config.PRECISE_SP3_MIRRORS)
            products.append(precise.parse_sp3(pathlib.Path(p)))
            got.append(f"{w}:{dd}")
            if (w, dd) == (week, dow):
                central_ok = True
        except (precise.PreciseProductError, OSError):
            continue  # a neighbour day may simply not be published yet

    if not central_ok:
        msg = (f"precise: could not obtain an SP3 product for {start.date()} "
               f"(GPS week {week} day {dow})")
        if fallback:
            return [msg + "; used broadcast"]
        raise ephemeris_source.EphemerisModeError(msg)

    _precise_provider.set_product(precise.merge_sp3(products))
    st = _precise_provider.status()
    audit.log_event("precise_autoload", source=st.get("source"),
                    satellites=st.get("satellites"), epochs=st.get("epochs"),
                    gps_week=week, dow=dow, days=",".join(got))
    src = st.get("source") or ""
    warns = [f"precise: auto-downloaded SP3 for GPS week {week} "
             f"day(s) {','.join(got)}"]
    if "ULT" in src:
        warns.append("precise: only an ultra-rapid (IGU) product was available "
                     "for this epoch; its predicted portion is less accurate "
                     "than rapid/final orbits")
    lo, hi = _precise_provider.product.coverage_seconds
    if not (lo + 9000.0 <= t_want <= hi - 9000.0):
        warns.append("precise: start is near an SP3 coverage edge; "
                     "interpolation window may be off-centre")
    return warns


def _precise_nav_override(body: dict, start: dt.datetime):
    """For ``ephemeris_mode == "precise"``: fit an SP3-backed broadcast
    record set for ``start`` and return ``(eph_dict, warnings)``. The
    fitted records -- unlike ``ephemeris.align_epochs`` -- carry solved
    M0/perturbations so the satellite states gps-sdr-sim propagates match
    the precise product to well under a metre.

    Returns ``(None, [])`` for broadcast mode. Raises
    ``ephemeris_source.EphemerisModeError`` (HTTP 422) when precise is
    requested but unavailable, unless ``fallback_to_broadcast`` is set.
    """
    mode = ephemeris_source.normalise_mode(body.get("ephemeris_mode"))
    if mode != "precise":
        return None, []
    fallback = bool(body.get("fallback_to_broadcast"))
    auto_warns = _ensure_precise_loaded(start, fallback)
    if not _precise_provider.loaded:
        # fallback path: auto-download disabled or failed, warning already set
        return None, auto_warns
    gps_start = start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    week, sow = ephemeris.gps_week_and_sow(gps_start)
    prns = sorted(p for (s, p) in _precise_provider.satellites() if s == "G")
    try:
        eph, warns = ephemeris_fit.build_precise_broadcast(
            _precise_provider, prns, GPSTime(week, sow), strict=not fallback)
    except precise.InterpolationWindowError:
        # merged SP3 still cannot centre the window (a neighbour day was not
        # published yet); accept an off-centre window with a warning rather
        # than fail the whole run.
        eph, warns = ephemeris_fit.build_precise_broadcast(
            _precise_provider, prns, GPSTime(week, sow), strict=not fallback,
            allow_boundary=True)
        warns = ["precise: SP3 window is off-centre near a coverage edge "
                 "(orbit interpolation slightly degraded)"] + warns
    except (ephemeris_fit.EphemerisFitError, precise.PreciseProductError) as e:
        if fallback:
            return None, [f"precise generation unavailable ({e}); used broadcast"]
        raise ephemeris_source.EphemerisModeError(f"precise generation: {e}")
    return eph, auto_warns + warns


@app.post("/api/generate")
def generate(body: dict):
    start = dt.datetime.fromisoformat(body["start_utc"])
    # Reject bad engine/fading before any ephemeris resolution so the caller
    # gets an explicit 422, never a silent coerce or an unrelated 503.
    engine = body.get("engine", "gps-sdr-sim")
    if engine not in ("gps-sdr-sim", "native"):
        raise HTTPException(422, f"engine: unknown {engine!r}")
    if body.get("fading") is not None:
        from backend.synth.fading import FadingConfig
        try:
            FadingConfig.from_dict(body["fading"])
        except ValueError as e:
            raise HTTPException(422, f"fading: {e}")
    if body.get("sample_format") == "int12" and engine != "native":
        raise HTTPException(422, "sample_format 'int12' requires engine 'native'")
    systems = body.get("systems", ["G"])
    if not isinstance(systems, list) or not all(
            isinstance(s, str) and s in signals.SYSTEMS for s in systems):
        raise HTTPException(422, f"systems: unknown entry in {systems!r}")
    if engine != "native" and sorted(set(systems)) != ["G"]:
        raise HTTPException(422,
            "gps-sdr-sim is GPS-only; use engine=native for other systems")
    nav_override, precise_warnings = _precise_nav_override(body, start)
    # In precise mode the broadcast nav file is never read; only resolve
    # (and possibly download) one when it will actually be used.
    rinex_path = (body.get("rinex_path") or "") if nav_override is not None \
        else _resolve_rinex(body, start)
    req = scenario.ScenarioRequest(
        rinex_path=rinex_path, lat=body["lat"], lon=body["lon"], alt=body["alt"],
        start=start, duration_s=int(body["duration_s"]),
        sample_rate=float(body.get("sample_rate", config.DEFAULT_SAMPLE_RATE)),
        sample_format=body.get("sample_format", "int16"),
        route=[tuple(p) for p in body["route"]] if body.get("route") else None,
        nav_override=nav_override,
        impairments=body.get("impairments"),
        random_seed=body.get("random_seed"),
        atmosphere=body.get("atmosphere"),
        receiver_clock=body.get("receiver_clock"),
        multipath=body.get("multipath"),
        models_to_iq=bool(body.get("models_to_iq")),
        engine=body.get("engine", "gps-sdr-sim"),
        fading=body.get("fading"),
        systems=systems,
    )
    if req.impairments is not None:
        try:
            impairments.ImpairmentConfig.from_dict(req.impairments)
        except ValueError as e:
            raise HTTPException(422, f"impairments: {e}")
    try:
        channel_models.ChannelModels.from_request(req)  # validate dicts early
    except ValueError as e:
        raise HTTPException(422, f"channel model: {e}")
    if scenario.estimate_bytes(req) > download_free_bytes(config.OUT_DIR):
        raise HTTPException(507, "estimated IQ size exceeds free disk space")

    def events():
        try:
            q: list = []
            outdir = signal_engine.run(req, progress_cb=lambda f: q.append(f))
            for f in q:
                yield f"data: {json.dumps({'progress': f})}\n\n"
            # generator.run aligns every satellite's toc/toe to the request's
            # start (KNOWN_ISSUES F4) before handing the nav file to
            # gps-sdr-sim, so the IQ was generated from that aligned
            # ephemeris, not the original broadcast one. Build "expected"
            # from the same aligned copy here or this comparison reports
            # huge, misleading errors whenever the real epoch was far from
            # the requested start.
            if req.nav_override is not None:
                # Precise mode: the IQ was generated from the SP3-fitted
                # records, so build "expected" from those same records.
                eph = req.nav_override
            else:
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
                         "inspect": table,
                         "ephemeris_mode": "precise" if req.nav_override is not None else "broadcast",
                         "warnings": precise_warnings}}
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
def iqplot(outdir: str, n: int = 2000, offset: int = 0):
    od = config.OUT_DIR / outdir
    meta = json.loads((od / "meta.json").read_text())
    bin_path = od / "gpssim.bin"
    # spectrum() needs a full 4096-sample window (its default nfft) even when
    # the caller only wants a shorter waveform/constellation preview, or the
    # PSD gets zero-padded (widened main lobe, misleading plot) whenever n<4096.
    iq = inspector.read_iq(bin_path, meta["sample_format"],
                           max_samples=max(n, 4096), offset_samples=max(0, offset))
    freqs, power_db = inspector.spectrum(iq, meta["sample_rate"])
    return {
        "i": iq.real[:n].tolist(), "q": iq.imag[:n].tolist(),
        "spectrum_freq_hz": freqs.tolist(), "spectrum_db": power_db.tolist(),
        "offset": offset, "sample_rate": meta["sample_rate"],
        "sample_format": meta["sample_format"],
        "total_samples": inspector.iq_sample_count(bin_path, meta["sample_format"]),
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
def start_transmit(body: dict, request: Request):
    auth.require_operator(request)
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
            audit.log_event("transmit_start", slot=slot, iq_path=params.iq_path,
                             dry_run=body.get("dry_run", False), tx_gain_db=params.tx_gain_db)
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
            audit.log_event("transmit_finished", slot=slot)
            yield f"data: {json.dumps({'finished': True, 'slot': slot})}\n\n"
        finally:
            _release_tx_slot(slot)

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/transmit/stop")
def stop_transmit(request: Request, body: dict | None = None):
    auth.require_viewer_or_operator(request)  # a stop is safety-positive: any role may issue it
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
        audit.log_event("manual_stop", slot=slot)
    return {"stopped": True}


@app.get("/api/audit")
def audit_log(limit: int = 200):
    return {"events": audit.read_events(limit)}


@app.post("/api/device/connect")
def device_connect(body: dict, request: Request):
    """Open a standby control link to the SDR -- no RF until a transmit
    actually starts (backend/device.py)."""
    auth.require_operator(request)
    uri = body.get("uri") or config.DEVICE_URI
    try:
        entry = device.connect(uri)
    except device.DeviceError as e:
        raise HTTPException(502, str(e))
    audit.log_event("device_connect", uri=uri, info=entry.get("info", {}))
    return entry


@app.post("/api/device/disconnect")
def device_disconnect(body: dict, request: Request):
    auth.require_operator(request)
    uri = body.get("uri") or config.DEVICE_URI
    device.disconnect(uri)
    audit.log_event("device_disconnect", uri=uri)
    return {"uri": uri, "connected": False}


@app.get("/api/device/status")
def device_status():
    return {"devices": device.status()}


# --- Precise ephemeris (analysis only -- see docs/precise-ephemeris-design.md) ---

@app.get("/api/precise/status")
def precise_status():
    """What SP3 product, if any, is loaded, and its coverage."""
    return _precise_provider.status()


@app.post("/api/precise/load")
def precise_load(body: dict, request: Request):
    """Load a local SP3 file by path (operator). Downloads are only
    attempted when `download` is set AND PRECISE_SP3_MIRRORS is configured."""
    auth.require_operator(request)
    path = body.get("path")
    if body.get("download") and not path:
        dl = body["download"] if isinstance(body["download"], dict) else {}
        try:
            path = precise.download_sp3(int(dl["gps_week"]), int(dl["dow"]),
                                        config.PRECISE_DIR, config.PRECISE_SP3_MIRRORS)
        except (KeyError, ValueError) as e:
            raise HTTPException(400, f"bad download spec: {e}")
    if not path:
        raise HTTPException(400, "provide {\"path\": \"...\"} to a local SP3 file")
    if not pathlib.Path(path).is_file():
        raise HTTPException(404, f"no such SP3 file: {path}")
    _precise_provider.load(path)
    st = _precise_provider.status()
    audit.log_event("precise_load", source=st.get("source"),
                    satellites=st.get("satellites"), epochs=st.get("epochs"))
    return st


@app.post("/api/precise/compare")
def precise_compare(body: dict):
    """Per-PRN broadcast(realigned)-vs-precise state comparison at one epoch.

    'broadcast' here is the *aligned* ephemeris generator.run actually hands
    to gps-sdr-sim, so the deltas answer 'how far is the ephemeris that
    drives the IQ from the precise reference'."""
    if not _precise_provider.loaded:
        raise HTTPException(409, "no precise product loaded -- POST /api/precise/load first")
    start = dt.datetime.fromisoformat(body["start_utc"])
    rx = geometry.llh_to_ecef(body["lat"], body["lon"], body["alt"])
    tow = _gps_tow(start)
    eph, eph_src = _resolve_eph(start.date(), body.get("rinex_path"))
    gps_start = start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    week, sow = ephemeris.gps_week_and_sow(gps_start)
    eph_aligned = ephemeris.align_epochs(eph, week, sow)

    prns = sorted(eph_aligned)
    state_map, warnings = ephemeris_source.build_state_fns(
        "precise", prns, GPSTime(week, sow), eph_aligned,
        provider=_precise_provider, on_missing="skip",
        fallback_to_broadcast=bool(body.get("fallback_to_broadcast")))

    mask = body.get("mask_deg", 5.0)
    import numpy as _np

    def _rows_at(tow_e):
        out = []
        for prn, src in state_map.items():
            if not callable(src):
                continue  # PRN fell back to broadcast -- nothing precise to compare
            b_pos, b_vel, _tb, b_clk = geometry.solve_transmit_time(eph_aligned[prn], rx, tow_e)
            p_pos, p_vel, _tp, p_clk = geometry.solve_transmit_time(src, rx, tow_e)
            b_obs = geometry.observables(eph_aligned[prn], rx, tow_e)
            if b_obs["el_deg"] < mask:
                continue
            p_obs = geometry.observables(src, rx, tow_e)
            dpos = _np.asarray(p_pos) - _np.asarray(b_pos)
            radial = _np.asarray(b_pos) / _np.linalg.norm(b_pos)
            along = _np.asarray(b_vel) / _np.linalg.norm(b_vel)
            cross = _np.cross(radial, along)
            out.append({
                "prn": prn,
                "pos_delta_m": float(_np.linalg.norm(dpos)),
                "pos_delta_radial_m": float(dpos @ radial),
                "pos_delta_along_m": float(dpos @ along),
                "pos_delta_cross_m": float(dpos @ cross),
                "clock_delta_s": float(p_clk - b_clk),
                "range_delta_m": float(p_obs["geo_range_m"] - b_obs["geo_range_m"]),
                "pseudorange_delta_m": float(p_obs["pseudorange_m"] - b_obs["pseudorange_m"]),
                "doppler_delta_hz": float(p_obs["carrier_doppler_hz"] - b_obs["carrier_doppler_hz"]),
                "el_deg": float(b_obs["el_deg"]),
            })
        return out

    def _summary(rws):
        if not rws:
            return {}
        return {
            "n": len(rws),
            "pos_delta_rms_m": float(_np.sqrt(_np.mean([r["pos_delta_m"] ** 2 for r in rws]))),
            "range_delta_rms_m": float(_np.sqrt(_np.mean([r["range_delta_m"] ** 2 for r in rws]))),
            "doppler_delta_rms_hz": float(_np.sqrt(_np.mean([r["doppler_delta_hz"] ** 2 for r in rws]))),
        }

    rows = _rows_at(tow)
    rows.sort(key=lambda r: -r["el_deg"])

    # Optional time sweep: same comparison at t = 0, step, .. sweep_s past
    # the start, so the frontend can draw the error as a curve rather than
    # a single-epoch snapshot.
    series = {}
    sweep_s = int(body.get("sweep_s", 0) or 0)
    step_s = max(1, int(body.get("step_s", 300) or 300))
    if sweep_s > 0:
        for t_off in range(0, sweep_s + 1, step_s):
            for r in _rows_at(tow + t_off):
                series.setdefault(str(r["prn"]), []).append({"t_offset_s": t_off, **r})

    return {
        "epoch_utc": start.isoformat(), "broadcast_source": eph_src,
        "precise_source": _precise_provider.product.source,
        "note": "IQ generation uses the broadcast column; precise is the reference.",
        "warnings": warnings, "rows": rows, "summary": _summary(rows),
        "series": series, "sweep_s": sweep_s, "step_s": step_s if sweep_s else 0,
    }


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


def _apply_timeline_step(session: live.LiveSession, step: dict) -> None:
    """Run one scheduled timeline step against a live session -- the exact
    same jog()/shift_time() a manual /api/live/jog or /api/live/time_shift
    call would use, just triggered by elapsed time instead of an operator
    click."""
    action = step["action"]
    if action == "jog":
        session.jog(step["direction"], float(step["distance_m"]))
    elif action == "time_shift":
        session.shift_time(step["field"], float(step["delta"]))
    else:
        raise ValueError(f"unknown timeline action {action!r}")


@app.post("/api/live/start")
def live_start(body: dict, request: Request):
    auth.require_operator(request)
    if not config.ALLOW_TX or not body.get("confirm_isolated"):
        raise HTTPException(403, "transmit disabled: needs ALLOW_TX and confirm_isolated")
    slot = _acquire_tx_slot()
    try:
        start = dt.datetime.fromisoformat(body["start_utc"])
        nav_override, _ = _precise_nav_override(body, start)
        rinex_path = (body.get("rinex_path") or "") if nav_override is not None \
            else _resolve_rinex(body, start)
        req = scenario.ScenarioRequest(
            rinex_path=rinex_path, lat=body["lat"], lon=body["lon"], alt=body["alt"],
            start=start, duration_s=int(body.get("duration_s", 300)),
            sample_rate=float(body.get("sample_rate", config.DEFAULT_SAMPLE_RATE)),
            sample_format=body.get("sample_format", "int16"),
            nav_override=nav_override)
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

    max_duration_s = body.get("max_duration_s")
    max_duration_s = float(max_duration_s) if max_duration_s else None
    timeline = sorted(body.get("timeline") or [], key=lambda s: s["at_s"])
    recorder = recording.RecordingWriter(slot) if body.get("record") else None

    def events():
        try:
            started = time.monotonic()
            audit.log_event("live_start", slot=slot, lat=body["lat"], lon=body["lon"],
                             dry_run=body.get("dry_run", False), max_duration_s=max_duration_s)
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

            def emit(payload: dict) -> str:
                if recorder is not None:
                    recorder.append(payload)
                return f"data: {json.dumps(payload)}\n\n"

            timeline_idx = 0
            while th.is_alive() or q:
                while q:
                    yield emit({**q.pop(0), "slot": slot})
                elapsed = time.monotonic() - started
                while timeline_idx < len(timeline) and timeline[timeline_idx]["at_s"] <= elapsed:
                    step = timeline[timeline_idx]
                    try:
                        _apply_timeline_step(session, step)
                        audit.log_event("timeline_step", slot=slot, step=step)
                        yield emit({"timeline_step": step, "slot": slot})
                    except Exception as e:
                        audit.log_event("timeline_step_error", slot=slot, step=step, error=str(e))
                    timeline_idx += 1
                if _tx_slots[slot]["stop"].is_set():
                    break
                if max_duration_s is not None and elapsed >= max_duration_s:
                    _tx_slots[slot]["stop"].set()  # fail-safe: auto-stop after max_duration_s
                    audit.log_event("auto_stop_timeout", slot=slot, max_duration_s=max_duration_s)
                    break
                th.join(timeout=0.2)
            th.join(timeout=2.0)
            audit.log_event("live_finished", slot=slot)
            yield emit({"finished": True, "slot": slot})
        finally:
            if recorder is not None:
                recorder.close()
                audit.log_event("recording_saved", slot=slot, name=recorder.name)
            _release_tx_slot(slot)

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/api/recording/list")
def recording_list():
    return {"names": recording.list_names()}


@app.get("/api/recording/replay")
def recording_replay(name: str, speed: float = 1.0):
    try:
        events_ = recording.read_events(name)
    except FileNotFoundError:
        raise HTTPException(404, f"no recording named {name!r}")

    def gen():
        prev_t = 0.0
        for row in events_:
            t = row.pop("t", 0.0)
            gap = max(0.0, (t - prev_t) / max(speed, 1e-6))
            if gap:
                time.sleep(min(gap, 5.0))  # cap so a bad recording can't stall replay
            prev_t = t
            yield f"data: {json.dumps(row)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/receiver/listen")
def receiver_listen(body: dict, request: Request):
    auth.require_operator(request)
    try:
        receiver_feed.start_listen(body["mode"], **{k: v for k, v in body.items() if k != "mode"})
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"listening": True, "mode": body["mode"]}


@app.post("/api/receiver/stop_listen")
def receiver_stop_listen(request: Request):
    auth.require_operator(request)
    receiver_feed.stop_listen()
    return {"listening": False}


@app.get("/api/receiver/fix")
def receiver_fix_live():
    return {"listening": receiver_feed.is_listening(), "fix": receiver_feed.latest_fix()}


@app.post("/api/receiver/inject")
def receiver_inject(body: dict, request: Request):
    """Feed one NMEA sentence directly, bypassing serial/UDP -- for
    testing the closed-loop UI without a physical receiver attached."""
    auth.require_operator(request)
    parsed = receiver_feed.inject(body["sentence"])
    if parsed is None:
        raise HTTPException(400, "unrecognized or malformed NMEA sentence")
    return {"fix": parsed}


def _session_for_slot(slot: str) -> live.LiveSession:
    occ = _tx_slots.get(slot)
    if not occ or occ.get("session") is None:
        raise HTTPException(404, f"no live session on {slot}")
    return occ["session"]


@app.post("/api/live/jog")
def live_jog(body: dict, request: Request):
    auth.require_operator(request)
    session = _session_for_slot(body["slot"])
    session.jog(body["direction"], float(body["distance_m"]))
    return {"llh": session.state.llh}


@app.post("/api/live/time_shift")
def live_time_shift(body: dict, request: Request):
    auth.require_operator(request)
    s = _session_for_slot(body["slot"])
    s.shift_time(body["field"], float(body["delta"]))
    return {"time_offset_s": s.state.time_offset_s}


@app.post("/api/live/stop")
def live_stop(body: dict, request: Request):
    auth.require_viewer_or_operator(request)  # a stop is safety-positive: any role may issue it
    slot = body["slot"]
    occ = _tx_slots.get(slot)
    if occ:
        occ["stop"].set()
        if occ["session"]:
            occ["session"].stop()
        audit.log_event("manual_stop", slot=slot)
    return {"stopped": True}


@app.post("/api/trajectory/save")
def trajectory_save(body: dict, request: Request):
    auth.require_operator(request)
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


@app.post("/api/scenario/save")
def scenario_save(body: dict, request: Request):
    auth.require_operator(request)
    try:
        scenario_lib.save(body["name"], body.get("params", {}))
    except KeyError as e:
        raise HTTPException(400, f"missing field: {e}")
    except scenario_lib.ScenarioLibError as e:
        raise HTTPException(400, str(e))
    return {"saved": body["name"]}


@app.get("/api/scenario/list")
def scenario_list():
    return {"names": scenario_lib.list_names()}


@app.get("/api/scenario/load")
def scenario_load(name: str):
    try:
        return {"params": scenario_lib.load(name)}
    except scenario_lib.ScenarioLibError as e:
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
