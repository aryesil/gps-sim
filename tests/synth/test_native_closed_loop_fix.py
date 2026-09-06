import datetime as dt
import json
import pathlib

from backend import config
from backend.analysis import receiver
from backend.ephem import ephemeris
from backend.scenario import ScenarioRequest
from backend.synth import engine

# brdc_full carries all 32 GPS PRNs -> a real constellation and geometry.
_RINEX = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_full.rnx")
_RX = (41.0082, 28.9784, 100.0)


def test_closed_loop_fix_from_lnav_decoded_out_of_native_iq(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    start = dt.datetime(2021, 12, 11, 11, 59, 42)     # fixture broadcast epoch
    req = ScenarioRequest(
        rinex_path=_RINEX, lat=_RX[0], lon=_RX[1], alt=_RX[2], start=start,
        duration_s=20, sample_rate=2_600_000.0, sample_format="int16",
        engine="native", systems=("G",), nav_message=True)
    outdir = engine.run(req)
    meta = json.loads((outdir / "meta.json").read_text())
    assert meta["provenance"]["nav"] == "lnav"

    gps_start = start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    _week, sow = ephemeris.gps_week_and_sow(gps_start)

    res = receiver.fix_from_iq(
        outdir / "gpssim.bin", "int16", 2_600_000.0,
        eph_by_prn={},                       # empty: force the nav decode path
        approx_time_gps=sow, marker_llh=_RX,
        decode_nav=True)

    assert "error" not in res, res
    assert sum(1 for v in res["nav_decode"].values() if v == "ok") >= 4
    assert res["error_m"] < 150.0
