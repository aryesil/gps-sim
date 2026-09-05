import pathlib

import numpy as np

from backend import ephemeris, geometry
from backend.synth import _lib, engine

_RINEX = pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_sample.rnx"


def _one_eph():
    eph = ephemeris.parse_rinex(str(_RINEX))
    prn = sorted(eph)[0]
    return eph[prn]


def test_cpp_sat_state_matches_geometry_submm():
    e = _one_eph()
    ks = engine.kepler_struct(e)
    t0 = e["toe"]
    max_dp = max_dv = max_dc = 0.0
    for dtk in np.linspace(-1800, 1800, 25):
        t = t0 + float(dtk)
        pos = (_lib.c_double * 3)()
        vel = (_lib.c_double * 3)()
        clk = _lib.c_double()
        _lib.load_lib().synth_sat_state(ks, t, pos, vel, clk)
        p_py, v_py, c_py = geometry.sat_state(e, t)
        dp = np.linalg.norm(np.array(list(pos)) - p_py)
        dv = np.linalg.norm(np.array(list(vel)) - v_py)
        dc = abs(float(clk.value) - c_py)
        max_dp, max_dv, max_dc = max(max_dp, dp), max(max_dv, dv), max(max_dc, dc)
        assert dp < 1e-3
        assert dv < 1e-4
        assert dc < 1e-12
    print(f"max residuals: pos={max_dp:.3e} m  vel={max_dv:.3e} m/s  clk={max_dc:.3e} s")
