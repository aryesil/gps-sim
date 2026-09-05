"""Dense, independent post-fit validation of the SP3 -> broadcast fit.

The optimiser minimises its own vectorised Kepler model at 97 points.
``ephemeris_fit.evaluate_fit`` re-checks the fitted record with
``geometry.sat_state`` (the production propagator) on a denser grid that is
offset from the optimiser's, and decomposes the error into radial /
along-track / cross-track. These tests pin that the two agree and that the
strict gate fires on the dense metric.
"""
import json
import pathlib

import numpy as np
import pytest

from backend import ephemeris_fit, geometry
from backend.gpstime import GPSTime
from backend.precise import PreciseEphemerisProvider

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
WEEK, TOE = 2433, 475200.0


def _kepler_eph():
    return {
        "m0": 0.3, "e": 0.006, "sqrtA": 5153.7, "delta_n": 4.9e-9,
        "i0": 0.97, "idot": -2.5e-10, "omega0": -0.55, "omega_dot": -8.1e-9,
        "omega": 0.72, "cuc": 1.2e-6, "cus": 7.5e-6, "crc": 240.0, "crs": 18.0,
        "cic": -1.0e-7, "cis": 8.0e-8, "toe": TOE, "toc": TOE, "gps_week": WEEK,
        "af0": 1.5e-4, "af1": 2.0e-12, "af2": 0.0, "tgd": 0.0,
    }


def test_dense_eval_of_a_perfect_record_is_near_zero():
    truth = _kepler_eph()
    d = ephemeris_fit.evaluate_fit(
        truth, lambda s: geometry.sat_state(truth, float(s)),
        GPSTime(WEEK, TOE), window_s=14400.0)
    assert d["pos_3d_m"]["max"] < 1e-6
    for axis in ("radial_m", "along_track_m", "cross_track_m"):
        assert d[axis]["max"] < 1e-6
    assert d["clock_s"]["max"] < 1e-12


def test_fit_reports_dense_rac_breakdown_and_gate_uses_it():
    prov = PreciseEphemerisProvider()
    prov.load(str(FIX / "igs_sample.sp3"))
    truth = json.loads((FIX / "igs_sample_truth.json").read_text())
    eph, _ = ephemeris_fit.build_precise_broadcast(
        prov, truth["prns"], GPSTime(truth["gps_week"], truth["toe_sow"]),
        pos_tol_m=5.0)
    for rec in eph.values():
        f = rec["_fit"]
        assert "dense" in f
        dz = f["dense"]
        # dense grid is genuinely separate from the optimiser grid
        assert dz["n_dense"] != f["n_samples"]
        # RAC parts cannot each exceed the 3D magnitude
        for axis in ("radial_m", "along_track_m", "cross_track_m"):
            assert dz[axis]["max"] <= dz["pos_3d_m"]["max"] + 1e-9
        # reported headline residual IS the dense production-path max
        assert f["max_pos_resid_m"] == dz["pos_3d_m"]["max"]
        # sum-of-squares of RAC rms ~ 3D rms (orthonormal decomposition)
        rss = np.sqrt(sum(dz[a]["rms"] ** 2 for a in
                          ("radial_m", "along_track_m", "cross_track_m")))
        assert rss == pytest.approx(dz["pos_3d_m"]["rms"], rel=0.05)


def test_strict_gate_rejects_on_dense_residual_with_diagnostic_message():
    prov = PreciseEphemerisProvider()
    prov.load(str(FIX / "igs_sample.sp3"))
    truth = json.loads((FIX / "igs_sample_truth.json").read_text())
    with pytest.raises(ephemeris_fit.EphemerisFitError) as ei:
        ephemeris_fit.build_precise_broadcast(
            prov, truth["prns"][:1],
            GPSTime(truth["gps_week"], truth["toe_sow"]),
            pos_tol_m=0.001, strict=True)
    msg = str(ei.value)
    for token in ("dense post-fit", "threshold", "fit interval", "source"):
        assert token in msg
