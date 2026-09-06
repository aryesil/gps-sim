"""Live session: jog and time-shift must not create discontinuities in
the segment anchors, and the GPS-time-of-week shift must be a real,
consistent shift (position stays put, time advances by exactly delta).
"""
import datetime as dt

import numpy as np
import pytest

from backend import geometry, scenario
from backend.session import live


def _base_req():
    return scenario.ScenarioRequest(
        rinex_path="unused", lat=41.0, lon=29.0, alt=100.0,
        start=dt.datetime(2026, 1, 1), duration_s=60,
        sample_rate=2.6e6, sample_format="int16")


def test_repeated_jog_accumulates_without_position_steps():
    s = live.LiveSession(_base_req())
    anchors = [s.snapshot()]
    for _ in range(20):
        s.jog("east", 5.0)
        anchors.append(s.snapshot())
    gaps = [live.segment_boundary_gap(a, b)["position_gap_m"]
            for a, b in zip(anchors, anchors[1:])]
    # every step is one 5 m jog: ~5 m each, none wildly larger
    assert max(gaps) < 5.2
    assert min(gaps) > 4.8
    total = live.segment_boundary_gap(anchors[0], anchors[-1])["position_gap_m"]
    assert total == pytest.approx(100.0, rel=0.02)


def test_jog_then_time_shift_are_independent():
    s = live.LiveSession(_base_req())
    a0 = s.snapshot()
    s.shift_time("time_offset_s", 45.0)
    a1 = s.snapshot()
    gap = live.segment_boundary_gap(a0, a1)
    assert gap["position_gap_m"] < 1e-6          # time shift does not move us
    assert gap["time_gap_s"] == pytest.approx(45.0)


def test_time_offset_accumulates_monotonically():
    s = live.LiveSession(_base_req())
    offs = []
    for _ in range(10):
        s.shift_time("time_offset_s", 12.0)
        offs.append(s.snapshot()["time_offset_s"])
    assert offs == pytest.approx([12.0 * k for k in range(1, 11)])


def test_boundary_gap_measures_both_axes():
    prev = {"ecef": [0.0, 0.0, 0.0], "time_offset_s": 0.0}
    cur = {"ecef": [3.0, 4.0, 0.0], "time_offset_s": 2.5}
    g = live.segment_boundary_gap(prev, cur)
    assert g["position_gap_m"] == pytest.approx(5.0)
    assert g["time_gap_s"] == pytest.approx(2.5)


def test_snapshot_ecef_matches_llh():
    s = live.LiveSession(_base_req())
    s.jog("north", 250.0)
    s.jog("up", 30.0)
    snap = s.snapshot()
    assert np.allclose(snap["ecef"], geometry.llh_to_ecef(*snap["llh"]))
