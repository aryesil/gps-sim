import pathlib
import tempfile
import threading
import time

import numpy as np
import pytest

from backend.session import live
from backend import scenario


@pytest.fixture(autouse=True)
def _in_process_segments(monkeypatch):
    # the fakes below patch this process; the worker process would not see them
    monkeypatch.setattr(live.LiveSession, "use_process", False)


def _base_req():
    import datetime as dt
    return scenario.ScenarioRequest(
        rinex_path="unused", lat=0.0, lon=0.0, alt=0.0,
        start=dt.datetime(2024, 1, 1), duration_s=60,
        sample_rate=2.6e6, sample_format="int16")


def test_resolve_band_file_picks_the_one_present(tmp_path):
    (tmp_path / "gpssim_l2.bin").write_bytes(b"\x00")
    assert live._resolve_band_file(tmp_path) == "gpssim_l2.bin"


def test_resolve_band_file_rejects_no_known_file(tmp_path):
    with pytest.raises(RuntimeError, match="no known IQ output"):
        live._resolve_band_file(tmp_path)


def test_resolve_band_file_rejects_more_than_one(tmp_path):
    (tmp_path / "gpssim.bin").write_bytes(b"\x00")
    (tmp_path / "gpssim_g1.bin").write_bytes(b"\x00")
    with pytest.raises(RuntimeError, match="exactly one RF output"):
        live._resolve_band_file(tmp_path)


def test_segments_resolves_band_file_once_and_reuses_it(monkeypatch):
    """The band file is resolved from the first segment's outdir and then
    reused (not re-resolved) on later segments -- a real live session's
    engine/systems/bands never change mid-run, only position/time do."""
    s = live.LiveSession(_base_req())
    seen_paths = []

    def fake_run_segment(base_req, llh, time_offset_s, duration_s):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "gpssim_l5.bin").write_bytes(b"\x00\x00" * 100)
        return d
    _patch_engine(monkeypatch, s, fake_run_segment)

    def fake_read_iq(path, fmt):
        seen_paths.append(pathlib.Path(path).name)
        return np.zeros(50, dtype=np.complex64)
    monkeypatch.setattr(live.inspector, "read_iq", fake_read_iq)

    gen = s.segments()
    next(gen)
    next(gen)
    s.stop()
    with pytest.raises(StopIteration):
        next(gen)
    assert seen_paths == ["gpssim_l5.bin", "gpssim_l5.bin"]
    assert s._iq_filename == "gpssim_l5.bin"


def test_segments_raises_immediately_on_multi_band_conflict_no_retry(monkeypatch):
    """A systems/bands combo needing >1 RF output can never succeed on
    retry -- it must stop the session on the first segment, not burn
    through the transient-error retry budget first."""
    s = live.LiveSession(_base_req())
    calls = {"n": 0}

    def fake_run_segment(base_req, llh, time_offset_s, duration_s):
        calls["n"] += 1
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "gpssim.bin").write_bytes(b"\x00\x00" * 100)
        (d / "gpssim_g1.bin").write_bytes(b"\x00\x00" * 100)
        return d
    _patch_engine(monkeypatch, s, fake_run_segment)

    gen = s.segments()
    with pytest.raises(RuntimeError, match="exactly one RF output"):
        next(gen)
    assert calls["n"] == 1
    assert s.running is False


def test_jog_north_increases_latitude():
    s = live.LiveSession(_base_req())
    before = list(s.state.llh)
    s.jog("north", 1000.0)
    assert s.state.llh[0] > before[0]
    assert abs(s.state.llh[1] - before[1]) < 1e-6
    assert abs(s.state.llh[2] - before[2]) < 1.0


def test_jog_up_increases_altitude_only():
    s = live.LiveSession(_base_req())
    before = list(s.state.llh)
    s.jog("up", 50.0)
    assert s.state.llh[2] > before[2]
    assert abs(s.state.llh[0] - before[0]) < 1e-9
    assert abs(s.state.llh[1] - before[1]) < 1e-9


def test_shift_time_updates_field():
    s = live.LiveSession(_base_req())
    s.shift_time("time_offset_s", 30.0)
    assert s.state.time_offset_s == 30.0
    s.shift_time("time_offset_s", 30.0)
    assert s.state.time_offset_s == 60.0  # deltas accumulate


def test_shift_time_rejects_unknown_field():
    s = live.LiveSession(_base_req())
    with pytest.raises(ValueError):
        s.shift_time("not_a_field", 1.0)


def test_removed_time_fields_are_rejected():
    """pps_shift_s/clock_corr_ns were removed: pps_shift_s only duplicated
    time_offset_s and clock_corr_ns was never read by anything that
    produced signal. Rejecting them keeps the API honest."""
    s = live.LiveSession(_base_req())
    for field in ("pps_shift_s", "clock_corr_ns"):
        with pytest.raises(ValueError):
            s.shift_time(field, 1.0)


def test_segments_snapshot_is_consistent_under_concurrent_jog(monkeypatch):
    """Hammer jog() from another thread while segments() is mid-iteration;
    the snapshot each segment reads must never be a torn/partial state
    (dataclass field values must always be self-consistent numbers, never
    None or a mix of two different jogs half-applied)."""
    s = live.LiveSession(_base_req())

    call_count = {"n": 0}
    def fake_run_segment(base_req, llh, time_offset_s, duration_s):
        call_count["n"] += 1
        assert all(isinstance(v, float) for v in llh)
        assert isinstance(time_offset_s, float)
        import tempfile, pathlib
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "gpssim.bin").write_bytes(b"\x00\x00" * 100)
        return d
    _patch_engine(monkeypatch, s, fake_run_segment)
    monkeypatch.setattr(live.inspector, "read_iq",
                        lambda path, fmt: np.zeros(50, dtype=np.complex64))

    # Bounded, countable jogging: exactly NUM_JOGGERS threads each doing
    # JOGS_PER_THREAD jogs of a known distance, all racing on jog()'s
    # read-modify-write of self.state.llh concurrently with each other (not
    # just with segments()'s reader). This is what actually falsifies a
    # broken/removed lock: with a single jogger thread, jog()'s calls are
    # already serialized by construction and a missing lock would not lose
    # any updates (a bare list-object swap is already GIL-atomic against a
    # concurrent *reader*). Only concurrent *writers* racing on jog()'s
    # read-then-write can produce a lost update, which would show up as a
    # final displacement measurably less than
    # NUM_JOGGERS * JOGS_PER_THREAD * JOG_DISTANCE.
    NUM_JOGGERS = 2
    JOGS_PER_THREAD = 25
    JOG_DISTANCE = 10.0
    jog_count = {"n": 0}
    jog_count_lock = threading.Lock()  # protects the test's own counter only
    done_jogging = threading.Event()
    threads_remaining = {"n": NUM_JOGGERS}

    def jogger():
        for _ in range(JOGS_PER_THREAD):
            s.jog("north", JOG_DISTANCE)
            with jog_count_lock:
                jog_count["n"] += 1
        with jog_count_lock:
            threads_remaining["n"] -= 1
            if threads_remaining["n"] == 0:
                done_jogging.set()
    joggers = [threading.Thread(target=jogger, daemon=True) for _ in range(NUM_JOGGERS)]
    for t in joggers:
        t.start()

    gen = s.segments()
    for _ in range(20):
        next(gen)
    s.stop()
    done_jogging.wait(timeout=2.0)
    for t in joggers:
        t.join(timeout=2.0)
    assert call_count["n"] >= 20
    assert jog_count["n"] == NUM_JOGGERS * JOGS_PER_THREAD

    # Prove the lock actually mattered: with no lost updates, the final
    # position's displacement from the origin must match the sum of all
    # jogs from every thread (within a small curvature-tolerant slop), not
    # something measurably short of it as a lost read-modify-write between
    # two concurrent jog() calls would produce.
    start_ecef = np.array(live.geometry.llh_to_ecef(0.0, 0.0, 0.0))
    final_ecef = np.array(live.geometry.llh_to_ecef(*s.state.llh))
    displacement = np.linalg.norm(final_ecef - start_ecef)
    expected = NUM_JOGGERS * JOGS_PER_THREAD * JOG_DISTANCE
    assert abs(displacement - expected) < 1.0


def _patch_engine(monkeypatch, session, fake_run_segment):
    """Route LiveSession's per-segment native-engine call to a
    run_segment-shaped fake (llh, time offset from the session start)."""
    base = session.base_req

    def fake_run(req):
        off = float((req.start - base.start).total_seconds())
        return fake_run_segment(req, (float(req.lat), float(req.lon),
                                      float(req.alt)), off, req.duration_s)
    monkeypatch.setattr(live.signal_engine, "run", fake_run)


def test_segments_advance_time_and_pin_ephemeris(monkeypatch):
    """Each segment continues the previous one in GPS time (the old loop
    re-generated the same start every second, so the SDR replayed one
    0.9 s gps-sdr-sim clip forever), always on the native engine, with the
    ephemeris aligned to the SESSION start."""
    s = live.LiveSession(_base_req())
    reqs = []

    def fake_run(req):
        reqs.append(req)
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "gpssim.bin").write_bytes(b"\x00\x00" * 100)
        return d
    monkeypatch.setattr(live.signal_engine, "run", fake_run)
    monkeypatch.setattr(live.inspector, "read_iq",
                        lambda path, fmt: np.zeros(50, dtype=np.complex64))
    gen = s.segments()
    for _ in range(3):
        next(gen)
    s.stop()
    base = s.base_req
    offs = [(r.start - base.start).total_seconds() for r in reqs[:3]]
    assert offs == [0.0, 1.0, 2.0]
    assert all(r.engine == "native" and r.eph_epoch == base.start
               for r in reqs[:3])


def test_segment_ephemeris_refreshes_every_15_min():
    # constant within a 15 min block (seamless joins, cached parse),
    # re-pinned after it so a long session never broadcasts an expired
    # toe / GLONASS tb
    s = live.LiveSession(_base_req())
    base = s.base_req
    snap = live.LiveState(llh=[base.lat, base.lon, base.alt])
    ep = [(s._segment_request(k, snap).eph_epoch - base.start).total_seconds()
          for k in (0, 899, 900, 7250)]
    assert ep == [0.0, 0.0, 900.0, 7200.0]
