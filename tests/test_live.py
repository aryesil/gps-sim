import threading
import time

import numpy as np
import pytest

from backend import live, scenario


def _base_req():
    import datetime as dt
    return scenario.ScenarioRequest(
        rinex_path="unused", lat=0.0, lon=0.0, alt=0.0,
        start=dt.datetime(2024, 1, 1), duration_s=60,
        sample_rate=2.6e6, sample_format="int16")


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
    monkeypatch.setattr(live.generator, "run_segment", fake_run_segment)
    monkeypatch.setattr(live.inspector, "read_iq",
                        lambda path, fmt: np.zeros(50, dtype=np.complex64))

    stop_jogging = threading.Event()
    def jogger():
        while not stop_jogging.is_set():
            s.jog("north", 10.0)
    t = threading.Thread(target=jogger, daemon=True)
    t.start()

    gen = s.segments()
    for _ in range(20):
        next(gen)
    s.stop()
    stop_jogging.set()
    t.join(timeout=2.0)
    assert call_count["n"] >= 20
