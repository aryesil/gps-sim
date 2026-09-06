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
    assert s["systems"] == ["G"]          # S2: product coverage surfaced
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


def test_compare_time_sweep_returns_per_prn_series():
    body = {**RX, "start_utc": TOE_UTC, "rinex_path": BRDC,
            "sweep_s": 600, "step_s": 300}
    j = client.post("/api/precise/compare", json=body).json()
    assert j["sweep_s"] == 600 and j["step_s"] == 300
    assert j["series"], j
    # every point carries the full breakdown, and t offsets are a subset of
    # the sweep grid (a PRN may drop below the mask partway through)
    grid = {0, 300, 600}
    full = [pts for pts in j["series"].values()
            if {p["t_offset_s"] for p in pts} == grid]
    assert full, j["series"]
    for p in full[0]:
        assert "pos_delta_m" in p and "pos_delta_radial_m" in p and "el_deg" in p


def test_compare_without_sweep_has_empty_series():
    body = {**RX, "start_utc": TOE_UTC, "rinex_path": BRDC}
    j = client.post("/api/precise/compare", json=body).json()
    assert j["series"] == {} and j["sweep_s"] == 0


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
    # The loaded fixture does not cover 2027; the precise path tries to
    # auto-download the right product, the offline stub makes that fail,
    # and with no fallback the request is a hard 422 (no silent broadcast).
    r = client.post("/api/preview", json={
        **RX, "start_utc": "2027-01-01T00:00:00", "rinex_path": BRDC,
        "ephemeris_mode": "precise"})
    assert r.status_code == 422
    d = r.json()["detail"].lower()
    assert "precise" in d and ("sp3" in d or "obtain" in d)


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
    ws = " ".join(r.json()["warnings"]).lower()
    assert "broadcast" in ws and ("fell back" in ws or "auto-download failed" in ws)


def test_preview_precise_multignss_includes_galileo(monkeypatch):
    # systems=["G","E"] + ephemeris_mode=precise: the multi-GNSS preview must
    # run every requested system off the precise product (state-fn override),
    # not fall back to a "precise is GPS-only" warning.
    import numpy as np
    from backend import app as appmod
    from backend import geometry

    rx = np.asarray(geometry.llh_to_ecef(RX["lat"], RX["lon"], RX["alt"]), float)
    sat_pos = tuple(rx * 2.6)  # roughly overhead, comfortably above the mask

    def _fake_satellites():
        return [("G", n) for n in range(1, 11)] + [("E", 11), ("E", 12)]

    def _fake_state_fns(provider, keys, week, sow):
        return ({k: (lambda sow: (sat_pos, (0.0, 0.0, 0.0), 0.0)) for k in keys},
                [])

    monkeypatch.setattr(appmod._precise_provider, "satellites", _fake_satellites)
    monkeypatch.setattr(appmod.ephemeris_source, "build_precise_state_fns",
                        _fake_state_fns)

    r = client.post("/api/preview", json={
        **RX, "start_utc": TOE_UTC, "rinex_path": BRDC,
        "ephemeris_mode": "precise", "systems": ["G", "E"]})
    assert r.status_code == 200, r.text
    j = r.json()
    assert any(s["sys"] == "E" for s in j["satellites"]), j
    assert any("precise" in w for w in j["warnings"]), j["warnings"]


def test_preview_precise_multignss_honours_mask_deg(monkeypatch):
    # B2: the precise multi-GNSS preview must use the request's mask_deg, not a
    # hardcoded 5 deg. Every fake SV sits ~overhead, so a 89.9 deg mask hides
    # them all while the default mask keeps them.
    import numpy as np
    from backend import app as appmod
    from backend import geometry

    rx = np.asarray(geometry.llh_to_ecef(RX["lat"], RX["lon"], RX["alt"]), float)
    sat_pos = tuple(rx * 2.6)  # roughly overhead (el ~ 90 deg)

    def _fake_satellites():
        return [("G", n) for n in range(1, 11)] + [("E", 11), ("E", 12)]

    def _fake_state_fns(provider, keys, week, sow):
        return ({k: (lambda sow: (sat_pos, (0.0, 0.0, 0.0), 0.0)) for k in keys},
                [])

    monkeypatch.setattr(appmod._precise_provider, "satellites", _fake_satellites)
    monkeypatch.setattr(appmod.ephemeris_source, "build_precise_state_fns",
                        _fake_state_fns)

    base = {**RX, "start_utc": TOE_UTC, "rinex_path": BRDC,
            "ephemeris_mode": "precise", "systems": ["G", "E"]}
    lo = client.post("/api/preview", json={**base, "mask_deg": 5.0})
    hi = client.post("/api/preview", json={**base, "mask_deg": 90.0})
    assert lo.status_code == 200 and hi.status_code == 200, (lo.text, hi.text)
    assert len(lo.json()["satellites"]) > 0
    assert len(hi.json()["satellites"]) == 0


def test_preview_precise_raises_when_required_gps_absent(monkeypatch):
    # S8: G is a required system; if the SP3 product carries no GPS the precise
    # path RAISES (like the broadcast path's require=("G",)), not warn-and-omit.
    from backend import app as appmod

    monkeypatch.setattr(appmod._precise_provider, "satellites",
                        lambda: [("E", 11), ("E", 12)])
    r = client.post("/api/preview", json={
        **RX, "start_utc": TOE_UTC, "rinex_path": BRDC,
        "ephemeris_mode": "precise", "systems": ["G", "E"]})
    assert r.status_code == 422, r.text
    assert "GPS" in r.json()["detail"]


def test_preview_precise_gps_only_product_warns(monkeypatch):
    # When only a GPS-only SP3 product is available (no multi-GNSS),
    # _ensure_precise_loaded should emit a warning about non-GPS fallback.
    from backend import app as appmod
    from backend import precise

    # Create a minimal GPS-only SP3Product
    def _fake_parse_sp3(path):
        gps_product = precise.SP3Product(
            source="GPS-ONLY-TEST",
            gps_week=2433,
            epoch_interval_s=900.0
        )
        # Add only GPS records (system "G")
        gps_product.records[("G", 1)] = [(475200.0, 1e7, 2e7, 3e7, 1e-6)]
        gps_product.epoch_times = [475200.0]
        return gps_product

    def _fake_download_sp3(gps_week, dow, cache_dir, mirrors, *,
                           want_multignss=False):
        return SP3  # return the fixture path so it exists

    monkeypatch.setattr(appmod.precise, "download_sp3", _fake_download_sp3)
    monkeypatch.setattr(appmod.precise, "parse_sp3", _fake_parse_sp3)

    # Trigger the download path to hit _ensure_precise_loaded with a fresh product.
    # Use a date outside the current loaded fixture to trigger auto-download.
    r = client.post("/api/preview", json={
        **RX, "start_utc": "2027-01-01T00:00:00", "rinex_path": BRDC,
        "ephemeris_mode": "precise", "fallback_to_broadcast": True})
    assert r.status_code == 200
    ws = r.json()["warnings"]
    assert any("covers GPS only" in w and "omitted" in w for w in ws), \
        f"Expected GPS-only coverage warning in {ws}"
