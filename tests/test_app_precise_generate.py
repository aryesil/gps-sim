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


def test_precise_auto_downloads_sp3_for_start_utc(no_sp3, monkeypatch):
    """With nothing loaded, precise mode fetches the right IGS product for
    the requested Start UTC (no manual path, no button) and builds the
    fitted nav from it."""
    calls = []

    def fake_dl(week, dow, cache_dir, mirrors):
        calls.append((week, dow))
        return SP3

    monkeypatch.setattr(app_mod.precise, "download_sp3", fake_dl)
    ov, warns = app_mod._precise_nav_override({"ephemeris_mode": "precise"}, START)
    # central day + the day either side are fetched and merged
    assert len(calls) == 3
    assert any(w == 2433 for w, _ in calls)               # GPS week of 2026-08-28
    assert set(ov) == set(range(1, 11))
    assert any("auto-downloaded" in w.lower() for w in warns)
    app_mod._precise_provider._sp3 = None


def test_precise_reuses_loaded_product_without_downloading(with_sp3, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not download when the loaded product covers the epoch")

    monkeypatch.setattr(app_mod.precise, "download_sp3", boom)
    ov, warns = app_mod._precise_nav_override({"ephemeris_mode": "precise"}, START)
    assert set(ov) == set(range(1, 11))
    assert not any("auto-downloaded" in w.lower() for w in warns)


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


def test_generate_precise_multi_threads_broadcast_rinex(with_sp3, monkeypatch):
    """Native precise multi-GNSS still needs the broadcast RINEX so
    engine.run can recover R-only FDMA channels (glo_k). The precise-multi
    payload must reach the engine with a non-empty rinex_path when the body
    supplies one -- GPS-only precise leaves it empty."""
    seen = {}

    def fake_run(req, progress_cb=None, binary=None):
        seen["req"] = req
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(app_mod.signal_engine, "run", fake_run)
    r = client.post("/api/generate", json={
        "start_utc": START.isoformat(), "duration_s": 5,
        "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "engine": "native", "systems": ["G", "R"],
        "ephemeris_mode": "precise", "rinex_path": BRDC})
    assert r.status_code == 200, r.text
    req = seen["req"]
    assert isinstance(req.nav_override, dict)
    assert "precise_provider" in req.nav_override
    assert req.rinex_path == BRDC


def test_generate_precise_multi_auto_rinex_still_resolves(with_sp3, monkeypatch):
    """Even with rinex_path 'AUTO', the precise-multi payload gets a real
    resolved broadcast path (not left empty) so GLONASS keeps glo_k."""
    seen = {}
    monkeypatch.setattr(app_mod, "_resolve_rinex", lambda body, start: BRDC)

    def fake_run(req, progress_cb=None, binary=None):
        seen["req"] = req
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(app_mod.signal_engine, "run", fake_run)
    r = client.post("/api/generate", json={
        "start_utc": START.isoformat(), "duration_s": 5,
        "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "engine": "native", "systems": ["G", "R"],
        "ephemeris_mode": "precise"})
    assert r.status_code == 200, r.text
    assert seen["req"].rinex_path == BRDC


def test_generate_broadcast_sets_no_override(with_sp3, monkeypatch):
    seen = {}
    monkeypatch.setattr(app_mod.generator, "run",
                        lambda req, progress_cb=None, binary=None: seen.setdefault("req", req)
                        or (_ for _ in ()).throw(RuntimeError("stop")))
    client.post("/api/generate", json={
        "start_utc": START.isoformat(), "duration_s": 5,
        "lat": 41.0, "lon": 29.0, "alt": 100.0, "rinex_path": BRDC})
    assert seen["req"].nav_override is None
