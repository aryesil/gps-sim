# tests/synth/test_beidou_time_scale.py
"""BeiDou records carry toe on the GPS scale (BDT + 14 s) inside the pipeline
and the nav encoders broadcast toe - 14 s. A receiver then evaluates
Omega = Omega0 + (Omegadot - We) * tk - We * toe_bdt, so the simulated
satellite must use toe_bdt in the earth-rotation term too; otherwise every
BeiDou satellite is rotated about Z by We * 14 s (~5 km at Ankara) against
what the receiver computes from the broadcast message."""
import math
import pathlib

import numpy as np

from backend import geometry
from backend.ephem import ephemeris

_MIXED = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_mixed.rnx")
_WE = 7.2921150e-5
_MU = 3.986004418e14
_BDT_LAG_S = 14.0


def _icd_position(rec: dict, toe_bdt: float, t_bdt: float):
    """BDS-SIS-ICD-B1I 5.2.4.12 for a MEO/IGSO satellite, written from the ICD,
    sharing nothing with geometry.py."""
    A = rec["sqrtA"] ** 2
    n = math.sqrt(_MU / A ** 3) + rec["delta_n"]
    tk = t_bdt - toe_bdt
    M = rec["m0"] + n * tk
    E = M
    for _ in range(40):
        E = M + rec["e"] * math.sin(E)
    nu = math.atan2(math.sqrt(1 - rec["e"] ** 2) * math.sin(E), math.cos(E) - rec["e"])
    phi = nu + rec["omega"]
    s2, c2 = math.sin(2 * phi), math.cos(2 * phi)
    u = phi + rec["cus"] * s2 + rec["cuc"] * c2
    r = A * (1 - rec["e"] * math.cos(E)) + rec["crs"] * s2 + rec["crc"] * c2
    i = rec["i0"] + rec["idot"] * tk + rec["cis"] * s2 + rec["cic"] * c2
    x, y = r * math.cos(u), r * math.sin(u)
    Om = rec["omega0"] + (rec["omega_dot"] - _WE) * tk - _WE * toe_bdt
    return np.array([x * math.cos(Om) - y * math.cos(i) * math.sin(Om),
                     x * math.sin(Om) + y * math.cos(i) * math.cos(Om),
                     y * math.sin(i)])


def _meo_records():
    eph = ephemeris.parse_rinex_multi(_MIXED, ("C",))
    return [rec for (_s, _p), rec in eph.items()
            if rec["i0"] > 0.7 and not (1 <= rec.get("prn", 0) <= 5)]


def test_python_matches_icd_with_toe_on_bdt():
    recs = _meo_records()
    assert recs
    for rec in recs:
        toe_bdt = rec["toe"] - _BDT_LAG_S         # record is on the GPS scale
        t_gps = rec["toe"] + 300.0
        got = geometry.sat_state(rec, t_gps)[0]
        want = _icd_position(rec, toe_bdt, t_gps - _BDT_LAG_S)
        assert np.max(np.abs(got - want)) < 1e-3, rec.get("prn")


def test_native_matches_icd_with_toe_on_bdt():
    from backend.synth import _lib, engine

    for rec in _meo_records():
        toe_bdt = rec["toe"] - _BDT_LAG_S
        t_gps = rec["toe"] + 300.0
        got = np.array(_lib.sat_state_sys(engine.kepler_struct(rec), 2, t_gps)[0])
        want = _icd_position(rec, toe_bdt, t_gps - _BDT_LAG_S)
        assert np.max(np.abs(got - want)) < 1e-3, rec.get("prn")


def test_other_systems_keep_toe_untouched():
    for sysc in ("G", "J", "E", "I"):
        assert geometry.SYS_PARAMS[sysc].get("toe_offset_s", 0.0) == 0.0
    assert geometry.SYS_PARAMS["C"]["toe_offset_s"] == _BDT_LAG_S
