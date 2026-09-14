"""End-to-end GPS L5: the native engine emits the L5 band, the receiver
acquires I5, wipes the NH10 secondary, decodes CNAV 10/11/30 straight out
of the IQ, reconstructs each satellite's ephemeris and solves a position
-- with no ephemeris handed in.

Like the L2C end-to-end test, the I5 XB initial states are seeded
deterministically (IS-GPS-200-M Table 3-Ia is not available offline), so
a few PRNs at a near-equal Doppler can cross-lock onto a sibling; the
receiver keeps only decodes whose CNAV PRN field matches, and the
position bound is a few hundred metres rather than the tens a real
receiver reaches. See the L2/L5 design doc.

The L5 chip rate is 10.23 Mcps, so this runs at 11 Msps and is slow.
"""
import datetime as dt
import pathlib

import pytest

from backend import config
from backend.analysis import receiver
from backend.ephem import ephemeris
from backend.scenario import ScenarioRequest
from backend.synth import engine

_RINEX = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_full.rnx")
_RX = (41.0082, 28.9784, 100.0)
# L5 needs ~25 Msps for the I5 main lobe (see test_native_l5_nav_in_iq);
# 26 s covers a full CNAV 10/11 pair plus acquisition slack. This run is
# large (~650 M samples) and slow -- minutes, like the L2C fix test.
_FS = 25_000_000.0
_DUR = 26


@pytest.fixture(scope="module")
def l5_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("l5_fix")
    _orig = config.OUT_DIR
    config.OUT_DIR = out
    try:
        start = dt.datetime(2021, 12, 11, 11, 59, 42)
        req = ScenarioRequest(
            rinex_path=_RINEX, lat=_RX[0], lon=_RX[1], alt=_RX[2], start=start,
            duration_s=_DUR, sample_rate=_FS, sample_format="int8",
            engine="native", systems=("G",), bands=["L5"], nav_message=True,
            # req.sample_rate only governs the L1 band; the L5 band's own
            # fs is req.l5_sample_rate (else fs_policy.band_floor, which
            # for GPS L5I alone floors to 20.5 Msps, not this module's
            # intended 25 Msps). Pin it explicitly so generation and the
            # receiver's decode call below agree on the actual fs.
            l5_sample_rate=_FS)
        outdir = engine.run(req)
    finally:
        config.OUT_DIR = _orig
    gps_start = start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    _week, sow = ephemeris.gps_week_and_sow(gps_start)
    return outdir, sow


def test_meta_records_cnav_on_l5(l5_run):
    import json
    outdir, _sow = l5_run
    meta = json.loads((outdir / "meta.json").read_text())
    assert meta["provenance"]["nav"].get("G/L5") == "cnav"


def test_closed_loop_fix_from_cnav_decoded_out_of_native_l5_iq(l5_run):
    outdir, sow = l5_run
    res = receiver.fix_from_iq(
        outdir / "gpssim_l5.bin", "int8", _FS,
        eph_by_prn={}, approx_time_gps=sow, marker_llh=_RX, band="L5")

    assert "error" not in res, res
    assert sum(1 for v in res["nav_decode"].values() if v == "ok") >= 4
    assert res["error_m"] < 300.0
    assert res["residual_rms_m"] < 120.0
