# tests/test_trajectory.py
import pytest

from backend import trajectory, config


def test_save_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(trajectory, "_DIR", tmp_path / "trajectories")
    wps = [{"lat": 1.0, "lon": 2.0, "alt": 3.0, "speed": 5.0, "accel": 1.0},
          {"lat": 1.1, "lon": 2.1, "alt": 3.0, "speed": 5.0, "accel": 1.0}]
    trajectory.save("test-route", wps)
    assert trajectory.load("test-route") == wps
    assert "test-route" in trajectory.list_names()


def test_save_rejects_single_waypoint(tmp_path, monkeypatch):
    monkeypatch.setattr(trajectory, "_DIR", tmp_path / "trajectories")
    with pytest.raises(trajectory.TrajectoryError):
        trajectory.save("too-short", [{"lat": 1, "lon": 2, "alt": 3, "speed": 1, "accel": 1}])


def test_load_missing_raises():
    with pytest.raises(trajectory.TrajectoryError):
        trajectory.load("does-not-exist-xyz")


def test_invalid_name_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(trajectory, "_DIR", tmp_path / "trajectories")
    with pytest.raises(trajectory.TrajectoryError):
        trajectory.save("../escape", [{"lat": 1, "lon": 2, "alt": 3, "speed": 1, "accel": 1}] * 2)
