import pathlib

import pytest
from fastapi.testclient import TestClient

from backend.app import app

client = TestClient(app)
FIXDIR = pathlib.Path(__file__).parent / "fixtures"
SP3 = str(FIXDIR / "igs_sample.sp3")
BRDC = str(FIXDIR / "brdc_sample.rnx")
RX = {"lat": 41.0082, "lon": 28.9784, "alt": 100.0}
TOE_UTC = "2026-08-28T11:59:42"        # maps to GPS week 2433, TOW 475200 exactly


@pytest.fixture(autouse=True)
def _load_sp3():
    r = client.post("/api/precise/load", json={"path": SP3})
    assert r.status_code == 200, r.text
    yield


def test_status_reports_coverage():
    s = client.get("/api/precise/status").json()
    assert s["loaded"] is True
    assert s["gps_week"] == 2433
    assert s["satellites"] == list(range(1, 11))
    assert s["coverage_start_utc"].startswith("2026-08-28")


def test_load_rejects_missing_file():
    r = client.post("/api/precise/load", json={"path": "/no/such/file.sp3"})
    assert r.status_code == 404


def test_load_download_requires_mirrors(monkeypatch):
    # With the mirror list explicitly emptied, an SP3 download is refused
    # rather than silently skipped.
    from backend import app as appmod
    monkeypatch.setattr(appmod.config, "PRECISE_SP3_MIRRORS", [])
    r = client.post("/api/precise/load",
                    json={"download": {"gps_week": 2433, "dow": 5}})
    assert r.status_code == 422
    assert "PRECISE_SP3_MIRRORS" in r.json()["detail"]


def test_load_download_delegates_to_fetcher(monkeypatch):
    # The default mirror list is populated, so an explicit download request
    # reaches precise.download_sp3; stub it out so no network is touched.
    from backend import app as appmod
    calls = {}

    def _fake(gps_week, dow, cache_dir, mirrors):
        calls["args"] = (gps_week, dow, list(mirrors))
        return SP3

    monkeypatch.setattr(appmod.precise, "download_sp3", _fake)
    r = client.post("/api/precise/load",
                    json={"download": {"gps_week": 2433, "dow": 5}})
    assert r.status_code == 200, r.text
    assert calls["args"][0] == 2433 and calls["args"][1] == 5
    assert calls["args"][2], "default mirror list must be non-empty"


def test_compare_at_toe_shows_small_bias_only():
    body = {**RX, "start_utc": TOE_UTC, "rinex_path": BRDC}
    j = client.post("/api/precise/compare", json=body).json()
    assert j["rows"], j
    assert j["note"].startswith("IQ generation uses the broadcast")
    # at the true toe the broadcast realignment is a no-op; deltas are just
    # the fixture's baked ~1.5 m position bias.
    assert j["summary"]["pos_delta_rms_m"] < 5.0
    for row in j["rows"]:
        assert 0.5 < row["pos_delta_m"] < 5.0
        assert abs(row["clock_delta_s"]) < 1e-4


def test_compare_off_toe_exposes_realignment_error():
    body = {**RX, "start_utc": "2026-08-28T15:30:00", "rinex_path": BRDC}
    j = client.post("/api/precise/compare", json=body).json()
    assert j["rows"]
    # hours from the real broadcast epoch, align_epochs degrades badly
    assert j["summary"]["pos_delta_rms_m"] > 100.0


def test_compare_409_when_no_product_loaded():
    # explicitly clear the process-wide provider
    from backend import app as appmod
    appmod._precise_provider._sp3 = None
    r = client.post("/api/precise/compare",
                    json={**RX, "start_utc": TOE_UTC, "rinex_path": BRDC})
    assert r.status_code == 409


def test_preview_precise_out_of_coverage_is_422_no_fallback():
    r = client.post("/api/preview", json={
        **RX, "start_utc": "2027-01-01T00:00:00", "rinex_path": BRDC,
        "ephemeris_mode": "precise"})
    assert r.status_code == 422
    assert "coverage" in r.json()["detail"].lower()


def test_preview_precise_in_coverage_reports_precise_source():
    r = client.post("/api/preview", json={
        **RX, "start_utc": TOE_UTC, "rinex_path": BRDC,
        "ephemeris_mode": "precise"})
    assert r.status_code == 200
    assert any("precise" in w for w in r.json()["warnings"])


def test_preview_default_mode_unchanged():
    r = client.post("/api/preview", json={
        **RX, "start_utc": TOE_UTC, "rinex_path": BRDC})
    assert r.status_code == 200
    j = r.json()
    assert any(w.startswith("ephemeris: ") for w in j["warnings"])
    assert not any("precise" in w for w in j["warnings"])


def test_preview_precise_explicit_fallback_when_out_of_coverage():
    r = client.post("/api/preview", json={
        **RX, "start_utc": "2027-01-01T00:00:00", "rinex_path": BRDC,
        "ephemeris_mode": "precise", "fallback_to_broadcast": True})
    assert r.status_code == 200
    assert any("FELL BACK" in w for w in r.json()["warnings"])
