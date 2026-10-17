# tests/test_geometry.py
import json
import math
import pathlib

import numpy as np
import pytest

from backend import geometry
from backend.ephem import ephemeris

FIXDIR = pathlib.Path(__file__).parents[1] / "fixtures"
RX_LLH = (41.0082, 28.9784, 100.0)
T_RX = 475200.0  # GPS TOW seconds, fixed for the fixture day (== fixture toe)


def test_llh_ecef_roundtrip_magnitude():
    x, y, z = geometry.llh_to_ecef(*RX_LLH)
    assert abs(math.sqrt(x * x + y * y + z * z) - 6371e3) < 30e3


def test_sat_state_radius_is_orbital():
    eph = ephemeris.parse_rinex(FIXDIR / "brdc_sample.rnx")
    rx = geometry.llh_to_ecef(*RX_LLH)
    entries = geometry.constellation(eph, rx, T_RX)
    top = max(entries, key=lambda ent: ent["el_deg"])
    e = eph[top["prn"]]
    pos, vel, clk = geometry.sat_state(e, e["toe"])
    r = np.linalg.norm(pos)
    assert 2.55e7 < r < 2.72e7            # GPS orbital radius ~26,560 km (near apogee for eccentric SVs)
    assert 2.5e3 < np.linalg.norm(vel) < 4.2e3
    assert abs(clk) < 1e-3


def test_velocity_matches_numeric_difference():
    eph = ephemeris.parse_rinex(FIXDIR / "brdc_sample.rnx")
    e = eph[sorted(eph)[0]]
    p0, _, _ = geometry.sat_state(e, e["toe"] - 0.5)
    p1, _, _ = geometry.sat_state(e, e["toe"] + 0.5)
    _, v, _ = geometry.sat_state(e, e["toe"])
    assert np.allclose((p1 - p0), v, rtol=0, atol=2.0)   # m/s over 1 s, <2 m error


def test_observables_are_physical():
    eph = ephemeris.parse_rinex(FIXDIR / "brdc_sample.rnx")
    rx = geometry.llh_to_ecef(*RX_LLH)
    entries = geometry.constellation(eph, rx, T_RX)
    top = max(entries, key=lambda ent: ent["el_deg"])
    obs = geometry.observables(eph[top["prn"]], rx, T_RX)
    assert -90 <= obs["az_deg"] <= 360
    assert -90 <= obs["el_deg"] <= 90
    assert 1.9e7 < obs["geo_range_m"] < 2.7e7
    assert 0 <= obs["code_phase_chips"] < 1023
    assert abs(obs["carrier_doppler_hz"]) < 6000


def test_observables_sv_clock_bias_shifts_pseudorange_by_c_times_bias():
    from backend import config
    eph = ephemeris.parse_rinex(FIXDIR / "brdc_sample.rnx")
    rx = geometry.llh_to_ecef(*RX_LLH)
    prn = max(geometry.constellation(eph, rx, T_RX),
              key=lambda e: e["el_deg"])["prn"]
    base = geometry.observables(eph[prn], rx, T_RX)
    biased = geometry.observables(eph[prn], rx, T_RX, sv_clock_bias_s=1e-8)
    assert biased["pseudorange_m"] == pytest.approx(
        base["pseudorange_m"] - config.C * 1e-8, abs=1e-6)


def test_constellation_multi_signal_list_emits_one_entry_per_signal():
    from backend.synth import signals
    eph = ephemeris.parse_rinex(FIXDIR / "brdc_sample.rnx")
    rx = geometry.llh_to_ecef(*RX_LLH)
    prn = max(geometry.constellation(eph, rx, T_RX),
              key=lambda e: e["el_deg"])["prn"]
    rec = dict(eph[prn])
    rec["system"] = "G"
    ents = geometry.constellation_multi(
        {("G", prn): rec}, rx, T_RX,
        lambda s: signals.signals_for(s, {"L1", "L2"}), mask_deg=0.0)
    assert sorted(e["signal_id"].band for e in ents) == ["L1", "L2"]
    assert all(e["sys"] == "G" and e["prn"] == prn for e in ents)


def test_constellation_matches_golden():
    golden = json.loads((FIXDIR / "known_geometry.json").read_text())
    eph = ephemeris.parse_rinex(FIXDIR / "brdc_sample.rnx")
    rx = geometry.llh_to_ecef(*RX_LLH)
    got = geometry.constellation(eph, rx, T_RX)
    assert [g["prn"] for g in got] == [g["prn"] for g in golden]
    for a, b in zip(got, golden):
        assert abs(a["geo_range_m"] - b["geo_range_m"]) < 1.0
        assert abs(a["carrier_doppler_hz"] - b["carrier_doppler_hz"]) < 0.5
        assert abs(a["code_phase_chips"] - b["code_phase_chips"]) < 1e-3


def test_as_state_fn_accepts_callable_and_matches_dict_path():
    eph = ephemeris.parse_rinex(FIXDIR / "brdc_sample.rnx")
    e = eph[sorted(eph)[0]]
    rx = geometry.llh_to_ecef(*RX_LLH)

    def state_fn(t_gps):
        return geometry.sat_state(e, t_gps)      # same numbers, callable shape

    obs_dict = geometry.observables(e, rx, T_RX)
    obs_call = geometry.observables(state_fn, rx, T_RX)
    assert obs_call["geo_range_m"] == pytest.approx(obs_dict["geo_range_m"], abs=1e-6)
    assert obs_call["carrier_doppler_hz"] == pytest.approx(
        obs_dict["carrier_doppler_hz"], abs=1e-6)


def test_solve_transmit_time_callable_path():
    eph = ephemeris.parse_rinex(FIXDIR / "brdc_sample.rnx")
    e = eph[sorted(eph)[0]]
    rx = geometry.llh_to_ecef(*RX_LLH)
    a = geometry.solve_transmit_time(e, rx, T_RX)
    b = geometry.solve_transmit_time(lambda t: geometry.sat_state(e, t), rx, T_RX)
    assert np.allclose(a[0], b[0], atol=1e-3)     # rotated position
    assert a[2] == pytest.approx(b[2], abs=1e-9)  # time of flight


def test_dop_small_case():
    # four unit LOS directions, tetrahedral-ish; DOP finite and > 1
    entries = [
        {"_los": [0, 0, 1]},
        {"_los": [0.94, 0, 0.34]},
        {"_los": [-0.47, 0.82, 0.34]},
        {"_los": [-0.47, -0.82, 0.34]},
    ]
    d = geometry.dop(entries, rx_ecef=(0, 0, 0))
    assert 1.0 < d["pdop"] < 10.0
    assert d["gdop"] >= d["pdop"]


def test_enu_uses_geodetic_wgs84_latitude():
    """Verify I11: _enu computes up-vector from WGS-84 geodetic latitude, not geocentric."""
    lat_deg, lon_deg, h_m = 45.0, 30.0, 100.0
    rx_ecef = geometry.llh_to_ecef(lat_deg, lon_deg, h_m)
    e, n, u = geometry._enu(rx_ecef)

    # Expected geodetic normal vector on WGS-84 ellipsoid at (lat=45 deg, lon=30 deg)
    phi = math.radians(lat_deg)
    lam = math.radians(lon_deg)
    u_expected = np.array([math.cos(phi) * math.cos(lam),
                           math.cos(phi) * math.sin(lam),
                           math.sin(phi)])

    # Geocentric latitude would have angle arctan(z / sqrt(x^2 + y^2)) ~= 44.807 deg
    theta_geocentric = math.atan2(rx_ecef[2], math.hypot(rx_ecef[0], rx_ecef[1]))
    u_geocentric = np.array([math.cos(theta_geocentric) * math.cos(lam),
                             math.cos(theta_geocentric) * math.sin(lam),
                             math.sin(theta_geocentric)])

    # Angular error between u and expected geodetic normal should be nearly zero (< 1e-6 rad)
    angle_to_geodetic = math.acos(np.clip(np.dot(u, u_expected), -1.0, 1.0))
    angle_to_geocentric = math.acos(np.clip(np.dot(u, u_geocentric), -1.0, 1.0))
    assert math.degrees(angle_to_geodetic) < 1e-4
    assert math.degrees(angle_to_geocentric) > 0.18  # Proves difference from old geocentric bug

    # Verify ecef_to_llh roundtrip
    lat_out, lon_out, h_out = geometry.ecef_to_llh(*rx_ecef)
    assert abs(lat_out - lat_deg) < 1e-7
    assert abs(lon_out - lon_deg) < 1e-7
    assert abs(h_out - h_m) < 1e-3


def test_sat_state_clock_week_rollover_wrap():
    """Verify I15: sat_state wraps tsv across GPS week boundaries (t_gps near 0, toc near 604800)."""
    eph = {
        "system": "G",
        "prn": 1,
        "toe": 604750.0,
        "toc": 604750.0,
        "sqrtA": 5153.7,
        "e": 0.001,
        "m0": 0.0,
        "omega0": 0.0,
        "i0": math.radians(55.0),
        "omega": 0.0,
        "idot": 0.0,
        "omega_dot": 0.0,
        "delta_n": 0.0,
        "cuc": 0.0, "cus": 0.0, "crc": 0.0, "crs": 0.0, "cic": 0.0, "cis": 0.0,
        "af0": 1.0e-5,
        "af1": 1.0e-11,
        "af2": 0.0,
    }
    # t_gps is at 50 s into the new week; toc was 604750 s in the previous week (dt = +100 s)
    t_gps = 50.0
    _pos, _vel, clk = geometry.sat_state(eph, t_gps)

    # With rollover wrap: tsv = +100 s -> clk ~= af0 + af1 * 100 s
    # Without rollover wrap: tsv would be -604700 s -> clk error would be ~6e-6 s (1.8 km)
    expected_clk = eph["af0"] + eph["af1"] * 100.0
    assert abs(clk - expected_clk) < 1e-8

