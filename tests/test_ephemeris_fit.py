import json
import pathlib

import numpy as np
import pytest

from backend import ephemeris, ephemeris_fit, geometry
from backend.gpstime import GPSTime
from backend.precise import PreciseEphemerisProvider

FIX = pathlib.Path(__file__).parent / "fixtures"
WEEK = 2433
TOE = 475200.0


def _kepler_eph():
    # A physically plausible GPS broadcast record (SI units, rad).
    return {
        "m0": 0.3, "e": 0.006, "sqrtA": 5153.7, "delta_n": 4.9e-9,
        "i0": 0.97, "idot": -2.5e-10, "omega0": -0.55, "omega_dot": -8.1e-9,
        "omega": 0.72, "cuc": 1.2e-6, "cus": 7.5e-6, "crc": 240.0, "crs": 18.0,
        "cic": -1.0e-7, "cis": 8.0e-8, "toe": TOE, "toc": TOE, "gps_week": WEEK,
        "af0": 1.5e-4, "af1": 2.0e-12, "af2": 0.0, "tgd": 0.0,
    }


def test_fit_recovers_a_pure_kepler_track_to_millimetres():
    truth = _kepler_eph()

    def state_fn(sow):
        return geometry.sat_state(truth, float(sow))

    fitted = ephemeris_fit.fit_satellite(
        state_fn, GPSTime(WEEK, TOE), prn=7, source="synthetic",
        window_s=14400.0)

    assert fitted["_fit"]["max_pos_resid_m"] < 1e-3

    # Independent check at times between the fit samples.
    for off in (-3000.0, -137.0, 512.0, 2800.0):
        want, _, _ = geometry.sat_state(truth, TOE + off)
        got, _, _ = geometry.sat_state(fitted, TOE + off)
        assert np.linalg.norm(np.asarray(want) - np.asarray(got)) < 1e-2


def test_fit_clock_matches_quadratic_baseline():
    truth = _kepler_eph()

    def state_fn(sow):
        return geometry.sat_state(truth, float(sow))

    fitted = ephemeris_fit.fit_satellite(
        state_fn, GPSTime(WEEK, TOE), prn=7, source="synthetic")
    # Compare the evaluated clock (af0 also absorbs bias, and sat_state
    # re-adds the relativistic term from the fitted orbit).
    for off in (-1800.0, 0.0, 1800.0):
        want = geometry.sat_state(truth, TOE + off)[2]
        got = geometry.sat_state(fitted, TOE + off)[2]
        assert abs(want - got) < 1e-9


def test_fitted_record_serialises_to_rinex2(tmp_path):
    truth = _kepler_eph()
    fitted = ephemeris_fit.fit_satellite(
        lambda s: geometry.sat_state(truth, float(s)),
        GPSTime(WEEK, TOE), prn=7, source="synthetic")
    fitted.pop("_fit")
    text = ephemeris.to_rinex2_nav({7: fitted})
    # 2 header lines + 8 lines per satellite record.
    assert len(text.splitlines()) == 10
    assert "RINEX VERSION / TYPE" in text


def test_build_precise_broadcast_from_sp3_fixture_beats_realignment():
    prov = PreciseEphemerisProvider()
    prov.load(str(FIX / "igs_sample.sp3"))
    truth = json.loads((FIX / "igs_sample_truth.json").read_text())
    prns = truth["prns"]

    eph, warnings = ephemeris_fit.build_precise_broadcast(
        prov, prns, GPSTime(truth["gps_week"], truth["toe_sow"]),
        pos_tol_m=5.0)

    assert set(eph) == set(prns)
    worst = max(e["_fit"]["max_pos_resid_m"] for e in eph.values())
    # The fixture is Kepler propagation plus a small per-PRN ECEF bias; a
    # broadcast fit absorbs almost all of it.
    assert worst < 5.0
    assert any("fit:" in w for w in warnings)


def test_build_precise_broadcast_skips_prn_absent_from_product():
    prov = PreciseEphemerisProvider()
    prov.load(str(FIX / "igs_sample.sp3"))
    truth = json.loads((FIX / "igs_sample_truth.json").read_text())

    eph, warnings = ephemeris_fit.build_precise_broadcast(
        prov, truth["prns"] + [31], GPSTime(truth["gps_week"], truth["toe_sow"]),
        pos_tol_m=5.0)

    assert 31 not in eph
    assert any("PRN 31 absent" in w for w in warnings)


def test_build_precise_broadcast_raises_when_no_prn_in_product():
    prov = PreciseEphemerisProvider()
    prov.load(str(FIX / "igs_sample.sp3"))
    truth = json.loads((FIX / "igs_sample_truth.json").read_text())
    with pytest.raises(ephemeris_fit.EphemerisFitError):
        ephemeris_fit.build_precise_broadcast(
            prov, [31, 32], GPSTime(truth["gps_week"], truth["toe_sow"]))
