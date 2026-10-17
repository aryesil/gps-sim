from __future__ import annotations

import copy
import dataclasses
import math
import datetime as dt
import queue
import shutil
import threading
import time
from dataclasses import dataclass

import numpy as np

from backend import config, geometry, inspector, scenario
from backend.synth import signal_engine

_PREFETCH = 6   # segments generated ahead of the TX stream (in-memory ring buffer)
# Live ephemeris is re-pinned every 15 min of session time. Each pin picks
# every satellite's record nearest it and keeps real records untouched, so a
# pin only changes a satellite when a newer record is nearer: GPS (2 h
# records) stays within |t - toe| <= 1.25 h, GLONASS (30 min records) within
# |t - tb| <= 30 min, the age RTKLIB-style receivers still accept (a 2 h pin
# left GLONASS tb up to 3 h old, which they reject).
_EPH_REFRESH_S = 900.0

_ENU_DIRECTIONS = {
    "north": (0.0, 1.0, 0.0), "south": (0.0, -1.0, 0.0),
    "east": (1.0, 0.0, 0.0), "west": (-1.0, 0.0, 0.0),
    "up": (0.0, 0.0, 1.0), "down": (0.0, 0.0, -1.0),
}
# Only one time control is real: a GPS-time-of-week shift applied to the
# whole segment (start + nav epochs move together, see
# generator.run_segment). A separate PPS-phase / satellite-clock knob
# would need its own distinct effect on the generated signal; until one
# exists, nothing here pretends to offer it.
_TIME_FIELDS = {"time_offset_s"}

# Every RF-band output file the native/gps-sdr-sim engines can produce
# (backend.synth.bands.BAND_REGISTRY's out_file values, GLONASS's G1/G2
# included even though they aren't one of the user-facing "L1"/"L2"/"L5"
# band choices -- GLONASS rides its own file whenever it's in `systems`).
# A live TX stream feeds exactly one physical output, so exactly one of
# these may exist in a segment's outdir; see _resolve_band_file.
_BAND_FILES = ("gpssim.bin", "gpssim_g1.bin", "gpssim_g2.bin",
               "gpssim_l2.bin", "gpssim_l5.bin")


def _resolve_band_file(outdir) -> str:
    """Which of ``_BAND_FILES`` this segment actually produced.

    A live session's engine/systems/bands are fixed for its whole run (only
    position and time are live-adjustable), so this only needs to run once
    -- LiveSession caches the result after the first segment. Zero matches
    means the request selected a system RINEX has no records for; more than
    one means the systems/bands combination would need more than one
    physical RF output (e.g. GPS L1 + GLONASS, which sits on its own G1
    band) -- live transmit has exactly one output per channel, so that is
    rejected here rather than silently streaming just one of the bands."""
    present = [f for f in _BAND_FILES if (outdir / f).exists()]
    if not present:
        raise RuntimeError(
            "live segment produced no known IQ output file -- check that "
            "`systems`/`bands` actually have RINEX coverage for this run")
    if len(present) > 1:
        raise RuntimeError(
            "live transmit supports exactly one RF output per channel, but "
            f"this systems/bands selection produced {len(present)}: "
            f"{present!r} -- pick a single band (or drop an overlapping "
            "system, e.g. GLONASS alongside GPS on the same band) for Start")
    return present[0]


def _seed_worker_cache(cache_snapshot: dict) -> None:
    """Pre-seed the spawned worker process's RINEX parse cache so the first
    segment never stalls on a cold georinex.load()."""
    from backend.ephem import ephemeris
    ephemeris._PARSE_CACHE.update(cache_snapshot)


def _generate_segment_raw(seg_req, iq_filename):
    """One live segment's raw or IQ data (module-level so a worker process can run it).
    Returns (raw_or_iq, band file name, is_raw)."""
    res = signal_engine.run(seg_req)
    if isinstance(res, dict):
        if iq_filename is None:
            if len(res) > 1:
                raise RuntimeError(
                    "live transmit supports exactly one RF output per channel, but "
                    f"this systems/bands selection produced {len(res)}: "
                    f"{list(res.keys())!r} -- pick a single band for Start Now")
            iq_filename = next(iter(res.keys()))
        return (res[iq_filename], iq_filename, True)

    outdir = res
    try:
        if iq_filename is None:
            # A systems/bands combination needing >1 physical output is a
            # static config error, not a transient generation hiccup --
            # surface it immediately rather than burning through the retry
            # budget on a request that can never succeed.
            iq_filename = _resolve_band_file(outdir)
        return (inspector.read_iq(outdir / iq_filename, seg_req.sample_format),
                iq_filename, False)
    finally:
        if getattr(seg_req, "outdir", None) is None:
            shutil.rmtree(outdir, ignore_errors=True)


def _generate_segment(seg_req, iq_filename):
    """One live segment's IQ (module-level so a worker process can run it).
    Returns (iq, band file name)."""
    data, iq_filename, is_raw = _generate_segment_raw(seg_req, iq_filename)
    if is_raw:
        return (inspector.raw_to_iq(data), iq_filename)
    return (data, iq_filename)


def _generate_segment_air(seg_req, iq_filename, air_name, level_dbfs, tx_scale, gain):
    """One live segment, scaled, saturated and packed for the air (module-level
    so a worker process runs it, see backend.rf.air). Returns
    (AirChunk, band file name)."""
    from backend.rf import air
    data, iq_filename, _is_raw = _generate_segment_raw(seg_req, iq_filename)
    chunk = air.prepare(data, air.format_by_name(air_name), level_dbfs=level_dbfs,
                        tx_scale=tx_scale, gain=gain)
    return chunk, iq_filename


@dataclass
class LiveState:
    llh: list[float]
    time_offset_s: float = 0.0


class LiveSession:
    def __init__(self, base_req: scenario.ScenarioRequest, segment_duration_s: float = 1.0):
        self.base_req = base_req
        self.segment_duration_s = segment_duration_s
        self.state = LiveState(llh=[base_req.lat, base_req.lon, base_req.alt])
        self._lock = threading.Lock()
        self.running = False
        self.consecutive_errors = 0
        self._iq_filename: str | None = None   # resolved from the first segment
        # Start Now öncü süre ölçümü (bkz. lead_calib): oturum nesnesi, uygulamanın
        # "şimdi + lead" damgasından hemen sonra kurulur; ilk segment hazır
        # olunca aradaki süre first_segment_latency_s'e yazılır.
        self._created_mono = time.monotonic()
        self.first_segment_latency_s: float | None = None
        self._pool = None
        self._air = None          # (format, level_dbfs, tx_scale) once air_segments() is used
        self._air_gain = None
        self._workers = 1
        self._scratch_dir = None
        if base_req.nav_override is None and base_req.rinex_path:
            import pathlib as _pathlib
            if _pathlib.Path(base_req.rinex_path).is_file():
                try:
                    from backend.ephem import ephemeris
                    systems = tuple(dict.fromkeys(getattr(base_req, "systems", None) or ("G",)))
                    req_req = tuple(s for s in ("G",) if s in systems)
                    a_dt = base_req.start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
                    ephemeris.parse_rinex_multi(base_req.rinex_path, systems,
                                                require=req_req, at_gps=a_dt)
                except Exception:
                    pass

    def jog(self, direction: str, distance_m: float) -> None:
        if direction not in _ENU_DIRECTIONS:
            raise ValueError(f"unknown jog direction {direction!r}")
        e, n, u = _ENU_DIRECTIONS[direction]
        with self._lock:
            rx = np.array(geometry.llh_to_ecef(*self.state.llh))
            e_hat, n_hat, u_hat = geometry._enu(rx)
            delta = distance_m * (e * e_hat + n * n_hat + u * u_hat)
            new_ecef = rx + delta
            lat, lon, h = _ecef_to_llh(*new_ecef)
            self.state.llh = [lat, lon, h]

    def shift_time(self, field: str, delta: float) -> None:
        if field not in _TIME_FIELDS:
            raise ValueError(f"unknown time field {field!r}")
        with self._lock:
            setattr(self.state, field, getattr(self.state, field) + delta)

    # Generate segments in a worker PROCESS (production). In-thread
    # generation's pure-Python geometry/nav work held the GIL for up to
    # ~200 ms at a time and starved the TX pump thread, underrunning the
    # SDR (measured: 12 s of signal pushed in 21.5 s of wall time).
    use_process = True

    def _segment_request(self, k: int, snap: LiveState):
        seg = self.segment_duration_s
        base = self.base_req
        t = k * seg + snap.time_offset_s
        # Ephemeris is pinned per _EPH_REFRESH_S block of the session:
        # constant within a block so segments join seamlessly and the RINEX
        # parse stays cached, refreshed between blocks so a long session
        # never broadcasts an expired toe / tb.
        block = math.floor(t / _EPH_REFRESH_S) * _EPH_REFRESH_S
        return dataclasses.replace(
            base, lat=snap.llh[0], lon=snap.llh[1], alt=snap.llh[2],
            start=base.start + dt.timedelta(seconds=t),
            duration_s=seg, route=None, engine="native",
            eph_epoch=base.start + dt.timedelta(seconds=block),
            in_memory=True,
            skip_meta=True)

    def _make_segment(self, k: int, snap: LiveState):
        """Segment ``k`` of the session: GPS time base.start + k*seg (+ the
        operator's time shift). Always the native engine, with the
        ephemeris pinned per 15 min block (``eph_epoch``), so segment
        k+1 continues segment k sample-for-sample: code phase, nav data
        and secondary codes are clocked on absolute transmit time and the
        carrier phase is absolute (engine._trajectory_knots).

        gps-sdr-sim cannot do this: every run restarts its carrier phase at
        0 (gpssim.c ``phase_ini = 0.0; // TODO``), re-aligns the nav file's
        toe to the run start, and a ``-d 1`` run writes only 0.9 s. The old
        loop also never advanced the start time, so the SDR replayed the
        same 0.9 s forever -- a receiver saw time jump back every 0.9 s."""
        seg_req = self._segment_request(k, snap)
        if self._pool is not None:
            data, self._iq_filename, is_raw = self._pool.submit(
                _generate_segment_raw, seg_req, self._iq_filename).result()
            return inspector.raw_to_iq(data) if is_raw else data
        iq, self._iq_filename = _generate_segment(seg_req, self._iq_filename)
        return iq

    def air_segments(self, fmt, *, level_dbfs: float, tx_scale: float = 1.0):
        """Like segments(), but every chunk is an air.AirChunk: scaled to the
        TX level, saturated and packed in the worker process, so the process
        that runs the TX pump does no per-sample work. The digital gain is
        measured on the first segment and reused (as transmit.stream does)."""
        self._air = (fmt.name, float(level_dbfs), float(tx_scale))
        self._air_gain = None
        return self.segments()

    def _make_air_segment(self, k: int, snap: LiveState):
        seg_req = self._segment_request(k, snap)
        name, level_dbfs, tx_scale = self._air
        args = (seg_req, self._iq_filename, name, level_dbfs, tx_scale, self._air_gain)
        if self._pool is not None:
            chunk, self._iq_filename = self._pool.submit(_generate_segment_air, *args).result()
        else:
            chunk, self._iq_filename = _generate_segment_air(*args)
        self._air_gain = chunk.gain
        return chunk

    def segments(self):
        """Generator of complex IQ chunks -- transmit.stream()'s chunk_source
        for a live session. Stops (StopIteration) once self.running is set
        False by the caller (mirrors TxSession's cancel-event pattern but
        drives generation, not just playback).

        Segments are produced by a background thread up to ``_PREFETCH``
        ahead: the TX pump only buffers ~0.2 s, so generating segment k+1
        after segment k was queued would underflow (zero gaps = receiver
        clock jumps) every segment. Jog/time-shift therefore take effect
        a couple of segments later."""
        self.running = True
        out: queue.Queue = queue.Queue(maxsize=_PREFETCH)
        self._pool = None
        # a precise-ephemeris provider object need not pickle: stay in-process
        if self.use_process and self.base_req.nav_override is None:
            import concurrent.futures
            import multiprocessing
            from backend.ephem import ephemeris
            self._workers = max(1, int(config.LIVE_GEN_WORKERS)) if self._air else 1
            self._pool = concurrent.futures.ProcessPoolExecutor(
                max_workers=self._workers, mp_context=multiprocessing.get_context("spawn"),
                initializer=_seed_worker_cache,
                initargs=(dict(ephemeris._PARSE_CACHE),))

        def _put(item) -> bool:
            while self.running:
                try:
                    out.put(item, timeout=0.5)
                    return True
                except queue.Full:
                    pass
            return False

        def _producer_pipelined():
            """Air path with several workers: segment k+1..k+W are generated
            while k is still being sent, results are released in order. The
            first segment runs alone: it fixes the band file and the digital
            gain every later segment reuses."""
            import collections
            inflight: collections.deque = collections.deque()   # (k, snap, future)
            k_submit = 0
            errors = 0
            while self.running:
                while (len(inflight) < self._workers and self.running
                       and (inflight or k_submit == 0 or self._air_gain is not None)):
                    with self._lock:
                        snap = copy.deepcopy(self.state)
                    args = (self._segment_request(k_submit, snap), self._iq_filename,
                            *self._air[:3], self._air_gain)
                    try:
                        fut = self._pool.submit(_generate_segment_air, *args)
                    except RuntimeError:
                        if not self.running:      # pool shut down by stop()
                            return
                        raise
                    inflight.append((k_submit, snap, fut))
                    k_submit += 1
                    if k_submit == 1:
                        break          # wait for segment 0 before fanning out
                if not inflight:
                    return
                k, snap, fut = inflight[0]
                try:
                    chunk, self._iq_filename = fut.result()
                except RuntimeError as ex:
                    out.put(ex)
                    return
                except Exception as ex:
                    errors += 1
                    if errors >= 3:
                        out.put(ex)
                        return
                    # drop everything queued behind it and redo from k
                    for _k, _s, f in inflight:
                        f.cancel()
                    inflight.clear()
                    k_submit = k
                    continue
                errors = 0
                inflight.popleft()
                self.consecutive_errors = 0
                if self._air_gain is None:
                    self._air_gain = chunk.gain
                if k == 0 and self.first_segment_latency_s is None:
                    self.first_segment_latency_s = time.monotonic() - self._created_mono
                if not _put(chunk):
                    return

        def _producer():
            if self._air and self._pool is not None and self._workers > 1:
                return _producer_pipelined()
            k = 0
            while self.running:
                with self._lock:
                    snap = copy.deepcopy(self.state)
                try:
                    iq = (self._make_air_segment(k, snap) if self._air
                          else self._make_segment(k, snap))
                    self.consecutive_errors = 0
                    if k == 0 and self.first_segment_latency_s is None:
                        self.first_segment_latency_s = time.monotonic() - self._created_mono
                except RuntimeError as ex:
                    out.put(ex)
                    return
                except Exception as ex:
                    self.consecutive_errors += 1
                    if self.consecutive_errors >= 3:
                        out.put(ex)
                        return
                    continue   # retry the same k: time must not skip
                k += 1
                while self.running:
                    try:
                        out.put(iq, timeout=0.5)
                        break
                    except queue.Full:
                        pass

        th = threading.Thread(target=_producer, daemon=True)
        th.start()
        try:
            while self.running:
                try:
                    item = out.get(timeout=0.5)
                except queue.Empty:
                    if not th.is_alive():
                        return
                    continue
                if isinstance(item, Exception):
                    self.running = False
                    raise item
                yield item
        finally:
            self.running = False
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
            if getattr(self, "_scratch_dir", None) and self._scratch_dir.exists():
                shutil.rmtree(self._scratch_dir, ignore_errors=True)

    def stop(self) -> None:
        self.running = False
        if getattr(self, "_scratch_dir", None) and self._scratch_dir.exists():
            shutil.rmtree(self._scratch_dir, ignore_errors=True)

    def snapshot(self) -> dict:
        """Current segment anchor: receiver ECEF and the GPS-time-of-week
        offset applied to the next segment. Used to check that successive
        live segments join without a position or timing gap."""
        with self._lock:
            ecef = geometry.llh_to_ecef(*self.state.llh)
            return {"ecef": list(ecef), "llh": list(self.state.llh),
                    "time_offset_s": float(self.state.time_offset_s)}


def segment_boundary_gap(prev: dict, cur: dict) -> dict:
    """Discontinuity between two consecutive live-segment anchors.

    ``position_gap_m`` -- how far the receiver jumped between segments.
    ``time_gap_s``     -- change in the GPS-time-of-week offset.
    A caller enforces its own per-tick limits (e.g. a jog step cap and a
    max time nudge); this only measures."""
    p0 = np.array(prev["ecef"], float)
    p1 = np.array(cur["ecef"], float)
    return {
        "position_gap_m": float(np.linalg.norm(p1 - p0)),
        "time_gap_s": float(cur["time_offset_s"] - prev["time_offset_s"]),
    }


def _ecef_to_llh(x, y, z):
    # Same iterative WGS84 inverse as receiver.py:_ecef_to_llh -- duplicated
    # here (not imported) because receiver.py's version is private (leading
    # underscore) and this module has no other dependency on receiver.py;
    # promoting it to geometry.py is out of scope for this task.
    a, e2 = 6378137.0, 6.69437999014e-3
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1 - e2))
    for _ in range(6):
        nrad = a / np.sqrt(1 - e2 * np.sin(lat) ** 2)
        h = p / np.cos(lat) - nrad
        lat = np.arctan2(z, p * (1 - e2 * nrad / (nrad + h)))
    return float(np.degrees(lat)), float(np.degrees(lon)), float(h)
