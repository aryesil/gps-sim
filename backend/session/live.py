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


def _warm_worker() -> int:
    """Start-up work of a spawned worker done before Start Now commits to a
    play time: import the generation stack (numpy, the native engine, the
    command packer). Returns the worker's pid."""
    import os
    from backend.synth import engine  # noqa: F401
    try:
        from backend.fpga import stream  # noqa: F401
    except Exception:
        pass
    return os.getpid()


def _warm_pool(pool, workers: int) -> None:
    """Spawn every worker of ``pool`` and run ``_warm_worker`` in it (one
    task per worker submitted back to back: with no idle worker each submit
    spawns a new process)."""
    import concurrent.futures
    futs = [pool.submit(_warm_worker) for _ in range(max(1, int(workers)))]
    for f in concurrent.futures.as_completed(futs):
        f.result()


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


def _gnss_lead_samples(first_block: int, fs: float) -> int:
    """Samples between the whole-second plan start and block ``first_block``."""
    return (int(first_block) * 65536) % int(round(fs))


def _gnss_segment(seg_req, first_block, fs, level_dbfs, shift):
    """One FPGA command segment (module-level so a worker process can run it):
    plan the band, then pack its command buffers. Returns
    ``(buffers, info)``; buffers are picklable ``DmaBuffer`` objects."""
    from backend.fpga.stream import CommandStream
    from backend.synth import engine
    bands = engine.plan(seg_req).bands
    if len(bands) != 1:
        raise RuntimeError(
            "live transmit supports exactly one RF output per channel, "
            f"but this selection planned {len(bands)} bands -- pick a "
            "single band for Start")
    band = bands[0]
    if abs(band.fs - fs) > 1e-6:
        raise RuntimeError(f"planned band rate {band.fs} differs from the TX rate {fs}")
    cs = CommandStream(band, first_block=first_block, level_dbfs=level_dbfs,
                       cfg=(first_block == 0), cfg_nch="max", shift=shift,
                       sample_offset=_gnss_lead_samples(first_block, fs))
    info = {"nch": cs.nch, "nch_max": cs.channel_plan.nch_max, "dropped": len(cs.dropped)}
    return list(cs), info


class _NotRunning:
    """Event-like view of "the session was stopped" (cancels a play hold)."""

    def __init__(self, session):
        self._s = session

    def is_set(self) -> bool:
        return not self._s.running


@dataclass
class LiveState:
    llh: list[float]
    time_offset_s: float = 0.0


class LiveSession:
    def __init__(self, base_req: scenario.ScenarioRequest, segment_duration_s: float = 1.0,
                 schedule=None):
        self.base_req = base_req
        self.segment_duration_s = segment_duration_s
        # Start Now "schedule then play" (play_schedule.PlaySchedule): when set,
        # base_req.start is only provisional; the segment producers replace it
        # with the planned T_play once the slow start-up work is done, and the
        # transmit path holds the start command until T_play.
        self.schedule = schedule
        # Ephemeris pin anchor: block b of the session pins at _eph_base + b*900 s.
        # Fixed at creation (== base_req.start unless Start Now re-stamps the
        # start later), so the RINEX parse seeded below -- and copied into every
        # worker at spawn, before T_play is known -- is the one segments hit.
        self._eph_base = base_req.start
        if base_req.osnma is not None:
            from backend.analysis.osnma import keys as osnma_keys
            from backend.synth import engine
            cfg, _ = engine.osnma_parts(engine.osnma_abs(base_req.osnma, 0))
            osnma_keys.load_or_create(cfg.keyset_path or osnma_keys.DEFAULT_PATH)
            om = dict(base_req.osnma)
            om.setdefault("anchor_llh", [base_req.lat, base_req.lon, base_req.alt])
            self.base_req = base_req = dataclasses.replace(base_req, osnma=om)
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

    def _osnma_fields(self, t0: float, dur: float, phys_block: float) -> dict:
        """Request fields that keep the broadcast nav words (and so the OSNMA
        tags) on one ephemeris block per 30 s GST subframe: the words switch
        at the first subframe boundary after the 15 min block change, while
        the physical observables switch with ``eph_epoch`` as before.
        ``t0``/``dur`` are the session-time span the nav stream must cover."""
        base = self.base_req
        if base.osnma is None:
            return {}
        from backend.synth import engine
        gst0 = engine.gst_abs_of(base.start)

        def switch(n):
            g = gst0 + n * _EPH_REFRESH_S
            return n * _EPH_REFRESH_S + (math.ceil(g / 30.0) * 30.0 - g)

        lo = t0 - 4.0
        m = max(0, math.floor(lo / _EPH_REFRESH_S))
        while m > 0 and switch(m) > lo:
            m -= 1
        om = engine.osnma_abs(base.osnma, int(gst0))
        om["data_start_gst"] = (float("-inf") if m == 0
                                else float(round(gst0 + switch(m))))
        out = {}
        if m * _EPH_REFRESH_S != phys_block:
            om["eph_epoch_now"] = self._eph_base + dt.timedelta(seconds=m * _EPH_REFRESH_S)
        if switch(m + 1) <= t0 + dur:
            out["eph_epoch_next"] = self._eph_base + dt.timedelta(seconds=(m + 1) * _EPH_REFRESH_S)
            out["eph_switch_at"] = base.start + dt.timedelta(seconds=switch(m + 1))
        out["osnma"] = om
        return out

    def _segment_request(self, k: int, snap: LiveState, seg: float | None = None):
        seg = self.segment_duration_s if seg is None else seg
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
            eph_epoch=self._eph_base + dt.timedelta(seconds=block),
            in_memory=True,
            skip_meta=True, **self._osnma_fields(t, seg, block))

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

    def gnss_bands(self, fs: float, segment_blocks: int):
        """Generator of ``(first_block, BandPlan)`` for the FPGA engine ('gnss'
        format): consecutive plans of ``segment_blocks`` whole 65536-sample
        blocks (no trailing partial block), block indices continuing across
        plans. Only the plan is computed here; the engine synthesises. Runs
        until ``self.running`` is cleared (stop()) -- jog/time shifts apply to
        the next plan, like segments(). Raises if the request needs more than
        one RF band."""
        from backend.synth import engine
        self.running = True
        block = 65536
        seg_s = segment_blocks * block / float(fs)
        k = 0
        try:
            while self.running:
                with self._lock:
                    snap = copy.deepcopy(self.state)
                req = self._segment_request(k, snap, seg_s)
                bands = engine.plan(req).bands
                if len(bands) != 1:
                    raise RuntimeError(
                        "live transmit supports exactly one RF output per channel, "
                        f"but this selection planned {len(bands)} bands -- pick a "
                        "single band for Start")
                band = bands[0]
                if abs(band.fs - fs) > 1e-6:
                    raise RuntimeError(f"planned band rate {band.fs} differs from "
                                       f"the TX rate {fs}")
                if k == 0 and self.first_segment_latency_s is None:
                    self.first_segment_latency_s = time.monotonic() - self._created_mono
                yield k * segment_blocks, band
                k += 1
        finally:
            self.running = False

    def gnss_buffers(self, fs: float, segment_blocks: int, *, level_dbfs,
                     shift: int = 0, workers: int | None = None):
        """Generator of FPGA command ``DmaBuffer`` s for the live session,
        produced by worker PROCESSES (plan + command packing is pure Python
        and, in this process, starved the TX threads of the GIL: at 20 Msps with
        45 channels the feed ran at 80% of real time and the engine underran
        continuously). The first segment is ``segment_blocks`` long so TX can
        start quickly; later ones span about one second (the plan has a fixed
        cost per segment). Segments are generated up to ``workers`` ahead and
        released in order; jog / time shifts apply to the next submitted one."""
        import collections
        import concurrent.futures
        block = 65536
        first_n = int(segment_blocks)
        steady = max(int(segment_blocks), int(round(fs / block)))
        workers = max(1, int(workers if workers is not None else config.LIVE_GEN_WORKERS))
        self.running = True
        pool = self._make_pool(workers)
        inflight: collections.deque = collections.deque()
        first_block = 0

        def submit():
            nonlocal first_block
            n = first_n if first_block == 0 else steady
            with self._lock:
                snap = copy.deepcopy(self.state)
            req = self._gnss_request(first_block, n, fs, snap)
            inflight.append(pool.submit(_gnss_segment, req, first_block, fs,
                                        level_dbfs, shift))
            first_block += n

        try:
            sched = self.schedule
            if sched is not None:
                # Start Now: spawn + import every worker before committing to a
                # time, then plan T_play from the first segment's generation
                # time; a first segment that comes too late is dropped and
                # re-planned (nothing is on air yet).
                t_w = sched.now_mono()
                _warm_pool(pool, workers)
                sched.warmup_s = sched.now_mono() - t_w
                while self.running:
                    self._set_play_start(sched.plan())
                    first_block = 0
                    t0 = sched.now_mono()
                    while len(inflight) < workers + 1 and self.running:
                        submit()
                    if not inflight:
                        break
                    inflight[0].result()
                    sched.record_gen(sched.now_mono() - t0)
                    if sched.on_time() or sched.last_attempt:
                        break
                    for f in inflight:
                        f.cancel()
                    concurrent.futures.wait(list(inflight))
                    inflight.clear()
            while self.running:
                while len(inflight) < workers + 1 and self.running:
                    submit()
                if not inflight:
                    break
                fut = inflight.popleft()
                bufs, info = fut.result()
                if self.first_segment_latency_s is None:
                    self.first_segment_latency_s = time.monotonic() - self._created_mono
                    self.gnss_info = info
                yield from bufs
        finally:
            self.running = False
            pool.shutdown(wait=False, cancel_futures=True)

    def _make_pool(self, workers: int):
        """Worker processes for segment generation ('spawn': same on Windows
        and Linux; each worker gets the parent's RINEX parse cache)."""
        import concurrent.futures
        import multiprocessing
        from backend.ephem import ephemeris
        return concurrent.futures.ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
            initializer=_seed_worker_cache, initargs=(dict(ephemeris._PARSE_CACHE),))

    def _set_play_start(self, t: dt.datetime) -> None:
        """Start Now: commit the session's time base to the planned T_play.
        Every segment (and its ephemeris pin) is relative to base_req.start, so
        this must happen before the first segment is submitted."""
        self.base_req = dataclasses.replace(self.base_req, start=t)

    def _gnss_request(self, first_block: int, n_blocks: int, fs: float, snap: LiveState):
        """Scenario request for ``n_blocks`` command blocks starting at block
        ``first_block`` (same time base and ephemeris pinning as
        ``_segment_request``)."""
        base = self.base_req
        block = 65536
        # A plan's code, secondary-code and data-bit phases are relative to its
        # start, so a segment's plan starts on the whole second at or before the
        # segment (a whole number of every signal's code / bit period) and the
        # command generator skips ``_gnss_lead_samples`` into it.
        fs_i = int(round(fs))
        whole_s = (first_block * block) // fs_i
        lead = _gnss_lead_samples(first_block, fs)
        t = whole_s + snap.time_offset_s
        eph_block = math.floor((t + lead / fs) / _EPH_REFRESH_S) * _EPH_REFRESH_S
        return dataclasses.replace(
            base, lat=snap.llh[0], lon=snap.llh[1], alt=snap.llh[2],
            start=base.start + dt.timedelta(seconds=t),
            duration_s=(lead + n_blocks * block + 2) / float(fs), route=None,
            engine="native",
            eph_epoch=self._eph_base + dt.timedelta(seconds=eph_block),
            in_memory=True, skip_meta=True,
            **self._osnma_fields(t + lead / fs, (n_blocks * block + 2) / float(fs),
                                 eph_block))

    def air_segments(self, fmt, *, level_dbfs: float, tx_scale: float = 1.0,
                     hold: bool = True):
        """Like segments(), but every chunk is an air.AirChunk: scaled to the
        TX level, saturated and packed in the worker process, so the process
        that runs the TX pump does no per-sample work. The digital gain is
        measured on the first segment and reused (as transmit.stream does)."""
        self._air = (fmt.name, float(level_dbfs), float(tx_scale))
        self._air_gain = None
        return self.segments(hold=hold)

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

    def segments(self, hold: bool = True):
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
            self._workers = max(1, int(config.LIVE_GEN_WORKERS)) if self._air else 1
            self._pool = self._make_pool(self._workers)

        def _put(item) -> bool:
            while self.running:
                try:
                    out.put(item, timeout=0.5)
                    return True
                except queue.Full:
                    pass
            return False

        def _producer_pipelined(k0: int = 0):
            """Air path with several workers: segment k+1..k+W are generated
            while k is still being sent, results are released in order. The
            first segment runs alone: it fixes the band file and the digital
            gain every later segment reuses."""
            import collections
            inflight: collections.deque = collections.deque()   # (k, snap, future)
            k_submit = k0
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

        def _producer(k0: int = 0):
            if self._air and self._pool is not None and self._workers > 1:
                return _producer_pipelined(k0)
            k = k0
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

        th = None
        try:
            first = None
            if self.schedule is not None:
                # Start Now: warm up, plan T_play, generate segment 0 for it
                # (re-planning while late), then hold it until T_play minus the
                # PC path's start latency. Later segments are generated during
                # the hold.
                first = self._scheduled_first_segment()
                if first is None:
                    return
            th = threading.Thread(target=_producer, args=(0 if first is None else 1,),
                                  daemon=True)
            th.start()
            if first is not None:
                # hold=False: the consumer holds instead (transmit.stream's
                # start_gate, after it has prepared the first samples)
                if hold and self.schedule.wait_and_fire(
                        None, cancel=_NotRunning(self)) is None:
                    return
                yield first
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

    def _scheduled_first_segment(self):
        """Segment 0 of a scheduled Start Now (PC-IQ path), generated for the
        planned T_play; re-planned while it comes too late. None if stopped."""
        sched = self.schedule
        t_w = sched.now_mono()
        if self._pool is not None:
            _warm_pool(self._pool, self._workers)
        sched.warmup_s = sched.now_mono() - t_w
        errors = 0
        while self.running:
            self._set_play_start(sched.plan())
            self._air_gain = None          # level measured on the segment that plays
            with self._lock:
                snap = copy.deepcopy(self.state)
            t0 = sched.now_mono()
            try:
                item = (self._make_air_segment(0, snap) if self._air
                        else self._make_segment(0, snap))
            except RuntimeError:
                raise
            except Exception:
                errors += 1
                if errors >= 3:
                    raise
                continue
            sched.record_gen(sched.now_mono() - t0)
            if sched.on_time() or sched.last_attempt:
                if self.first_segment_latency_s is None:
                    self.first_segment_latency_s = time.monotonic() - self._created_mono
                return item
        return None

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
