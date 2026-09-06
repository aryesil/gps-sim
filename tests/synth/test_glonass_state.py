import pathlib

import numpy as np

from backend import ephemeris
from backend.synth import glonass

_MIXED = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_mixed.rnx")


def _one():
    d = ephemeris.parse_rinex_multi(_MIXED, ("R",))
    return next(v for k, v in d.items() if k[0] == "R")


def test_state_at_toe_returns_broadcast_vector():
    r = _one()
    f = glonass.glonass_state(r)
    p, v, c = f(r["toe_ref"])
    # georinex already returns GLONASS X/V/A in SI (m, m/s, m/s^2) -- no scaling.
    assert np.allclose(p, [r["x_m"], r["y_m"], r["z_m"]], atol=1.0)
    assert np.allclose(v, [r["vx"], r["vy"], r["vz"]], atol=1e-3)


def test_orbit_radius_is_glonass_altitude():
    r = _one()
    f = glonass.glonass_state(r)
    p, _, _ = f(r["toe_ref"] + 600.0)
    radius = np.linalg.norm(p)
    assert 25.3e6 < radius < 25.7e6            # ~19100 km altitude + Re


def test_glonass_state_advances():
    r = _one()
    f = glonass.glonass_state(r)
    p_fwd, _, _ = f(r["toe_ref"] + 900.0)
    p0, _, _ = f(r["toe_ref"])
    assert np.linalg.norm(p_fwd - p0) > 1e5    # it actually moved


def test_glonass_integration_round_trips():
    r = _one()
    f = glonass.glonass_state(r)
    # forward 900 s from the broadcast epoch...
    p_fwd, v_fwd, _ = f(r["toe_ref"] + 900.0)
    # ...then integrate that state backward 900 s and expect the start back.
    fwd_rec = dict(r, x_m=p_fwd[0], y_m=p_fwd[1], z_m=p_fwd[2],
                   vx=v_fwd[0], vy=v_fwd[1], vz=v_fwd[2],
                   toe_ref=r["toe_ref"] + 900.0)
    p_back, _, _ = glonass.glonass_state(fwd_rec)(r["toe_ref"])
    p0, _, _ = f(r["toe_ref"])
    assert np.linalg.norm(p_back - p0) < 5.0


def test_glonass_clock_is_linear():
    r = _one()
    f = glonass.glonass_state(r)
    tau, gamma = r["tau"], r["gamma"]
    _, _, c0 = f(r["toe_ref"])
    _, _, c100 = f(r["toe_ref"] + 100.0)
    assert abs(c0 - (-tau)) < 1e-12
    assert abs(c100 - (-tau + gamma * 100.0)) < 1e-9
