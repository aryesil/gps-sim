# tests/test_geometry.py
import json
import math
import pathlib

import numpy as np
import pytest

from backend import geometry, ephemeris

FIXDIR = pathlib.Path(__file__).parent / "fixtures"
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
