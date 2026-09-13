"""End-to-end Galileo E5a-I: the native engine emits a mixed GPS + Galileo
L5-band IQ, the receiver acquires both families (I5 for GPS, E5a-I for
Galileo), decodes CNAV 10/11/30 and F/NAV word types 1/2/3 straight out of
the IQ, reconstructs each satellite's ephemeris and solves one joint
position fix -- with no ephemeris handed in.

The fixture RINEX (brdc_mixed.rnx) carries only 3 Galileo satellites, not
enough on their own for a 4-SV fix, so this scenario mixes Galileo E5a-I
with GPS L5 on the shared L5 RF band (both centre on 1176.45 MHz) --
exactly the scenario the engine's own nav_streams keying (and this
receiver path's (sys, prn) keying) exists for. See the L2/L5 design doc.

F/NAV pages are 10 s and cycle word types 1-4 (40 s full cycle); the
capture start is chosen so GST TOW is an exact multiple of 40 (page type
1 lands exactly at t=0), so 44 s of capture guarantees complete word
types 1, 2 and 3 appear regardless of acquisition warm-up. The L5 chip
rate is 10.23 Mcps, so this runs at 25 Msps and is slow.
"""
import datetime as dt
import pathlib

import pytest

from backend import config
from backend.analysis import receiver
from backend.ephem import ephemeris
from backend.scenario import ScenarioRequest
from backend.synth import engine

_MIXED = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_mixed.rnx")
# brdc_mixed.rnx carries only 3 GPS + 3 Galileo satellites total; at most 4
# of the 6 are ever simultaneously above 5 deg from any single receiver (a
# consequence of having so few SVs spread across real orbital planes, not a
# bug) -- this lat/lon/time was picked (by scanning the fixture's actual
# geometry) as one where all of G1/G2/G3 and one Galileo SV (E3) are up
# with comfortable margin (12-67 deg).
_RX = (-10.0, 180.0, 100.0)
_FS = 25_000_000.0
# 2026-08-31 00:00:22 UTC -> GPS sow 86440.0, an exact multiple of the
# F/NAV 40 s page cycle (see the module docstring).
_START = dt.datetime(2026, 8, 31, 0, 0, 22)
_DUR = 44


@pytest.fixture(scope="module")
def e5a_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("e5a_fix")
    _orig = config.OUT_DIR
    config.OUT_DIR = out
    try:
        req = ScenarioRequest(
            rinex_path=_MIXED, lat=_RX[0], lon=_RX[1], alt=_RX[2],
            start=_START, duration_s=_DUR, sample_rate=_FS,
            sample_format="int8", engine="native", systems=("G", "E"),
            bands=["L5"], nav_message=True)
        outdir = engine.run(req)
    finally:
        config.OUT_DIR = _orig
    gps_start = _START + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    _week, sow = ephemeris.gps_week_and_sow(gps_start)
    return outdir, sow


def test_meta_records_fnav_and_cnav_on_l5(e5a_run):
    import json
    outdir, _sow = e5a_run
    meta = json.loads((outdir / "meta.json").read_text())
    assert meta["provenance"]["nav"].get("E/L5") == "fnav"
    assert meta["provenance"]["nav"].get("G/L5") == "cnav"


def test_closed_loop_fix_from_fnav_and_cnav_decoded_out_of_native_l5_iq(e5a_run):
    outdir, sow = e5a_run
    res = receiver.fix_from_iq(
        outdir / "gpssim_l5.bin", "int8", _FS,
        eph_by_prn={}, approx_time_gps=sow, marker_llh=_RX, band="L5")

    assert "error" not in res, res
    assert sum(1 for v in res["nav_decode"].values() if v == "ok") >= 4
    assert any(p.startswith("E") for p in res["prns_used"]), res["prns_used"]
    assert res["error_m"] < 300.0
    assert res["residual_rms_m"] < 120.0
