"""Precise (SP3-fitted) signal generation wiring.

The full IQ round-trip is covered by tests/test_integration_generate.py
(xfail under pytest, F3); here the concern is that ephemeris_mode=precise
builds a fitted broadcast nav set and feeds it to the generator, that
broadcast mode is untouched, and that an unavailable precise product
fails loudly instead of silently dropping to broadcast.
"""
import datetime as dt
import pathlib

import pytest
from fastapi.testclient import TestClient

from backend import app as app_mod
from backend.app import app

client = TestClient(app)
FIXDIR = pathlib.Path(__file__).parent / "fixtures"
SP3 = str(FIXDIR / "igs_sample.sp3")
BRDC = str(FIXDIR / "brdc_sample.rnx")
START = dt.datetime(2026, 8, 28, 11, 59, 42)


@pytest.fixture
def with_sp3():
    r = client.post("/api/precise/load", json={"path": SP3})
    assert r.status_code == 200, r.text
    yield
    app_mod._precise_provider._sp3 = None


@pytest.fixture
def no_sp3():
    app_mod._precise_provider._sp3 = None
    yield


# ---- _precise_nav_override helper ------------------------------------------

def test_broadcast_mode_returns_no_override(no_sp3):
    ov, warns = app_mod._precise_nav_override({}, START)
    assert ov is None and warns == []
    ov, warns = app_mod._precise_nav_override({"ephemeris_mode": "broadcast"}, START)
    assert ov is None


def test_precise_without_product_raises(no_sp3):
    from backend.ephemeris_source import EphemerisModeError
    with pytest.raises(EphemerisModeError):
        app_mod._precise_nav_override({"ephemeris_mode": "precise"}, START)


def test_precise_without_product_falls_back_when_asked(no_sp3):
    ov, warns = app_mod._precise_nav_override(
        {"ephemeris_mode": "precise", "fallback_to_broadcast": True}, START)
    assert ov is None
    assert any("broadcast" in w for w in warns)


def test_precise_builds_fitted_nav_matching_the_product(with_sp3):
    ov, warns = app_mod._precise_nav_override({"ephemeris_mode": "precise"}, START)
    assert set(ov) == set(range(1, 11))
    worst = max(e["_fit"]["max_pos_resid_m"] for e in ov.values())
    assert worst < 5.0
    # Fitted records carry solved perturbations, not just a stamped toe.
    assert any(abs(e["crs"]) > 1e-6 or abs(e["cus"]) > 1e-9 for e in ov.values())


# ---- HTTP surface --------------------------------------------------------

def test_generate_precise_without_product_is_422(no_sp3):
    r = client.post("/api/generate", json={
        "start_utc": START.isoformat(), "duration_s": 5,
        "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "ephemeris_mode": "precise", "rinex_path": BRDC})
    assert r.status_code == 422
    assert "precise" in r.text.lower()


def test_generate_precise_feeds_nav_override_to_generator(with_sp3, monkeypatch):
    seen = {}

    def fake_run(req, progress_cb=None, binary=None):
        seen["req"] = req
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(app_mod.generator, "run", fake_run)
    r = client.post("/api/generate", json={
        "start_utc": START.isoformat(), "duration_s": 5,
        "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "ephemeris_mode": "precise", "rinex_path": BRDC})
    assert r.status_code == 200
    assert '"error": "stop after capture"' in r.text
    req = seen["req"]
    assert req.nav_override is not None
    assert set(req.nav_override) == set(range(1, 11))


def test_generate_broadcast_sets_no_override(with_sp3, monkeypatch):
    seen = {}
    monkeypatch.setattr(app_mod.generator, "run",
                        lambda req, progress_cb=None, binary=None: seen.setdefault("req", req)
                        or (_ for _ in ()).throw(RuntimeError("stop")))
    client.post("/api/generate", json={
        "start_utc": START.isoformat(), "duration_s": 5,
        "lat": 41.0, "lon": 29.0, "alt": 100.0, "rinex_path": BRDC})
    assert seen["req"].nav_override is None
