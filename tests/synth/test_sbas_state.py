"""SBAS gets its own ICAO Annex-10 ECEF propagator (Task 27).

``S`` records used to be routed through ``backend.synth.glonass`` (the PZ-90
RK4 integrator with J2 / centrifugal / Coriolis terms). SBAS broadcasts a
plain ECEF state + constant acceleration; the correct model is straight
constant-acceleration integration.
"""
import numpy as np
import pytest

from backend import geometry
from backend.synth import glonass, sbas

# A realistic GEO-ish state (ICAO Annex-10 SBAS message): position ~4.2e7 m,
# a small tangential velocity, a small radial acceleration.
_REC = {
    "x_m": 24000000.0, "y_m": -34000000.0, "z_m": 12000.0,
    "vx": 5.0, "vy": 3.5, "vz": 0.1,
    "ax": 1.0e-7, "ay": -2.0e-7, "az": 0.0,
    "tau": 3.0e-7, "gamma": 1.0e-12,
    "toe_ref": 172800.0,
}


def _rec(**over):
    r = dict(_REC)
    r.update(over)
    return r


def test_dt_zero_returns_seed_state_exactly():
    f = sbas.sbas_state(_rec())
    pos, vel, clk = f(_REC["toe_ref"])
    assert np.array_equal(pos, np.array([_REC["x_m"], _REC["y_m"], _REC["z_m"]]))
    assert np.array_equal(vel, np.array([_REC["vx"], _REC["vy"], _REC["vz"]]))
    assert clk == _REC["tau"]


def test_dt_zero_bit_stable_for_tau_free_record():
    """The existing native acquire fixtures carry tau == gamma == 0; dt == 0
    there must reproduce exactly what the old glonass routing returned."""
    rec = _rec(tau=0.0, gamma=0.0, vx=0.0, vy=0.0, vz=0.0, ax=0.0, ay=0.0, az=0.0)
    ps, vs, cs = sbas.sbas_state(rec)(rec["toe_ref"])
    pg, vg, cg = glonass.glonass_state(rec)(rec["toe_ref"])
    assert np.array_equal(ps, pg) and np.array_equal(vs, vg) and cs == cg


def test_closed_form_position_after_300s():
    dt = 300.0
    f = sbas.sbas_state(_rec())
    pos, vel, clk = f(_REC["toe_ref"] + dt)
    r0 = np.array([_REC["x_m"], _REC["y_m"], _REC["z_m"]])
    v0 = np.array([_REC["vx"], _REC["vy"], _REC["vz"]])
    a = np.array([_REC["ax"], _REC["ay"], _REC["az"]])
    exp = r0 + v0 * dt + 0.5 * a * dt * dt
    assert np.allclose(pos, exp, atol=1e-6)
    assert np.allclose(vel, v0 + a * dt, atol=1e-9)
    assert clk == pytest.approx(_REC["tau"] + _REC["gamma"] * dt)
    # a GEO barely moves in 5 min: within a few km of the seed.
    assert np.linalg.norm(pos - r0) < 5_000.0


def test_no_pz90_dynamics():
    """Over 300 s the ICAO model and the PZ-90 integrator must visibly
    diverge for a real orbital-radius state -- proof the J2 / centrifugal
    terms are gone."""
    rec = _rec()
    ps, _, _ = sbas.sbas_state(rec)(rec["toe_ref"] + 300.0)
    pg, _, _ = glonass.glonass_state(rec)(rec["toe_ref"] + 300.0)
    assert np.linalg.norm(ps - pg) > 1.0


def test_state_fn_for_routes_S_to_sbas_not_glonass():
    rec = _rec(system="S", prn=120)
    f = geometry.state_fn_for(rec)
    # closure lives in backend.synth.sbas, not backend.synth.glonass
    assert f.__module__ == "backend.synth.sbas"
    rec_r = _rec(system="R", prn=1)
    assert geometry.state_fn_for(rec_r).__module__ == "backend.synth.glonass"
