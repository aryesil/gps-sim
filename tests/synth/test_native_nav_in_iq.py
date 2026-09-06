import datetime as dt
import json
import pathlib

from backend import config, geometry, inspector
from backend.ephem import ephemeris
from backend.scenario import ScenarioRequest
from backend.synth import engine

_RINEX = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_sample.rnx")


def _req(nav_message=True, dur=4):
    return ScenarioRequest(
        rinex_path=_RINEX, lat=41.0, lon=29.0, alt=100.0,
        start=dt.datetime(2024, 1, 1, 0, 0, 0), duration_s=dur,
        sample_rate=2_600_000.0, sample_format="int16",
        engine="native", systems=("G",), nav_message=nav_message)


def test_meta_provenance_nav_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    m = json.loads((engine.run(_req()) / "meta.json").read_text())
    assert m["provenance"]["nav"] == "lnav"
    m2 = json.loads((engine.run(_req(nav_message=False)) / "meta.json").read_text())
    assert m2["provenance"]["nav"] == "none"


def test_acquisition_still_succeeds_with_nav_modulation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    req = _req(dur=4)
    outdir = engine.run(req)
    gps_start = req.start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    week, sow = ephemeris.gps_week_and_sow(gps_start)
    eph = ephemeris.align_epochs(ephemeris.parse_rinex(_RINEX), week, sow)
    rx = geometry.llh_to_ecef(req.lat, req.lon, req.alt)
    sats = geometry.constellation(eph, rx, sow + req.duration_s / 2.0)
    iq = inspector.read_iq(outdir / "gpssim.bin", "int16",
                           max_samples=int(req.sample_rate * 0.010))
    table = inspector.compare(iq, req.sample_rate, sats)
    assert len([r for r in table if r["metric_db"] > 12.0]) >= min(4, len(sats))
