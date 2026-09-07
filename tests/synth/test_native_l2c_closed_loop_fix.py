"""End-to-end GPS L2C: the native engine emits the L2 band, the receiver
acquires CM, decodes CNAV 10/11/30 straight out of the IQ, reconstructs
each satellite's ephemeris and solves a position -- with no ephemeris
handed in.

The CM ranging codes are seeded deterministically (IS-GPS-200 Table 3-IIa
initial states are not available offline); their imperfect cross-
correlation lets a few PRNs at a near-equal Doppler cross-lock onto a
sibling, so the receiver keeps only decodes whose CNAV PRN field matches,
and the position bound is a few hundred metres rather than the tens a
real receiver reaches. See the L2/L5 design doc.
"""
import datetime as dt
import json
import pathlib

import pytest

from backend import config
from backend.analysis import receiver
from backend.ephem import ephemeris
from backend.scenario import ScenarioRequest
from backend.synth import engine

_RINEX = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_full.rnx")
_RX = (41.0082, 28.9784, 100.0)


@pytest.fixture(scope="module")
def l2_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("l2c_fix")
    _orig = config.OUT_DIR
    config.OUT_DIR = out
    try:
        start = dt.datetime(2021, 12, 11, 11, 59, 42)
        req = ScenarioRequest(
            rinex_path=_RINEX, lat=_RX[0], lon=_RX[1], alt=_RX[2], start=start,
            duration_s=40, sample_rate=2_600_000.0, sample_format="int8",
            engine="native", systems=("G",), bands=["L2"], nav_message=True)
        outdir = engine.run(req)
    finally:
        config.OUT_DIR = _orig
    gps_start = start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    _week, sow = ephemeris.gps_week_and_sow(gps_start)
    return outdir, sow


def test_meta_records_cnav_on_l2(l2_run):
    outdir, _sow = l2_run
    meta = json.loads((outdir / "meta.json").read_text())
    assert meta["provenance"]["nav"].get("G/L2") == "cnav"


def test_closed_loop_fix_from_cnav_decoded_out_of_native_l2_iq(l2_run):
    outdir, sow = l2_run
    res = receiver.fix_from_iq(
        outdir / "gpssim_l2.bin", "int8", 2_600_000.0,
        eph_by_prn={}, approx_time_gps=sow, marker_llh=_RX, band="L2")

    assert "error" not in res, res
    assert sum(1 for v in res["nav_decode"].values() if v == "ok") >= 4
    assert res["error_m"] < 250.0
    assert res["residual_rms_m"] < 100.0
