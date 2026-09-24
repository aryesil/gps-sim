from __future__ import annotations

import copy
import dataclasses
import math
import datetime as dt
import queue
import shutil
import threading
from dataclasses import dataclass

import numpy as np

from backend import geometry, inspector, scenario
from backend.synth import signal_engine

_PREFETCH = 2   # segments generated ahead of the TX stream
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


def _generate_segment(seg_req, iq_filename):
    """One live segment's IQ (module-level so a worker process can run it).
    Returns (iq, band file name)."""
    outdir = signal_engine.run(seg_req)
    try:
        if iq_filename is None:
            # A systems/bands combination needing >1 physical output is a
            # static config error, not a transient generation hiccup --
            # surface it immediately rather than burning through the retry
            # budget on a request that can never succeed.
            iq_filename = _resolve_band_file(outdir)
        return (inspector.read_iq(outdir / iq_filename, seg_req.sample_format),
                iq_filename)
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


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
        self._pool = None

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
            eph_epoch=base.start + dt.timedelta(seconds=block))

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
            iq, self._iq_filename = self._pool.submit(
                _generate_segment, seg_req, self._iq_filename).result()
            return iq
        iq, self._iq_filename = _generate_segment(seg_req, self._iq_filename)
        return iq

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
            self._pool = concurrent.futures.ProcessPoolExecutor(
                max_workers=1, mp_context=multiprocessing.get_context("spawn"))

        def _producer():
            k = 0
            while self.running:
                with self._lock:
                    snap = copy.deepcopy(self.state)
                try:
                    iq = self._make_segment(k, snap)
                    self.consecutive_errors = 0
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

    def stop(self) -> None:
        self.running = False

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
