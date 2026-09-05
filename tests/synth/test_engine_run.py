import datetime as dt
import json
import pathlib

from backend import config, ephemeris, geometry, inspector
from backend.scenario import ScenarioRequest
from backend.synth import engine

_RINEX = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_sample.rnx")


def _req():
    return ScenarioRequest(
        rinex_path=_RINEX, lat=41.0, lon=29.0, alt=100.0,
        start=dt.datetime(2024, 1, 1, 0, 0, 0), duration_s=5,
        sample_rate=2_600_000.0, sample_format="int16")


def _mid_epoch(req):
    gps_start = req.start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    week, sow = ephemeris.gps_week_and_sow(gps_start)
    eph = ephemeris.align_epochs(ephemeris.parse_rinex(_RINEX), week, sow)
    rx = geometry.llh_to_ecef(req.lat, req.lon, req.alt)
    return eph, rx, sow + req.duration_s / 2.0


def test_engine_writes_bin_and_meta_with_expected_sample_count(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    req = _req()
    outdir = engine.run(req)
    assert (outdir / "gpssim.bin").exists()
    n = inspector.iq_sample_count(outdir / "gpssim.bin", "int16")
    assert abs(n - int(2_600_000.0 * 5)) <= 2_600_000  # within one second slack
    m = json.loads((outdir / "meta.json").read_text())
    assert m["provenance"]["engine"] == "native"
    assert m["sample_format"] == "int16"


def test_engine_output_acquires_visible_gps_prns(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    req = _req()
    outdir = engine.run(req)
    eph, rx, t_mid = _mid_epoch(req)
    sats = geometry.constellation(eph, rx, t_mid)
    iq = inspector.read_iq(outdir / "gpssim.bin", "int16",
                           max_samples=int(req.sample_rate * 0.010))
    table = inspector.compare(iq, req.sample_rate, sats)
    acquired = [r for r in table if r["metric_db"] > 12.0]
    assert len(acquired) >= min(4, len(sats))


def test_engine_code_phase_matches_geometry_convention(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    req = _req()
    outdir = engine.run(req)
    eph, rx, t_mid = _mid_epoch(req)
    sats = geometry.constellation(eph, rx, t_mid)
    iq = inspector.read_iq(outdir / "gpssim.bin", "int16",
                           max_samples=int(req.sample_rate * 0.010))
    table = inspector.compare(iq, req.sample_rate, sats)
    strongest = max(table, key=lambda r: r["metric_db"])
    acq = inspector.acquire(iq, req.sample_rate, strongest["prn"])
    geo_phase = geometry.observables(eph[strongest["prn"]], rx, t_mid)["code_phase_chips"]
    d = abs(acq["code_phase_chips"] - geo_phase)
    resid = min(d, 1023 - d)
    assert resid <= 2.0, (strongest["prn"], acq["code_phase_chips"], geo_phase, resid)
