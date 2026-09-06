"""Weighted least-squares solver: recovery, weighting, DOP, covariance."""
import math

import numpy as np
import pytest

from backend import config, geometry
from backend.models import wls

FIX = __import__("pathlib").Path(__file__).resolve().parent.parent / "fixtures" / "brdc_sample.rnx"
TOE = 475200.0


@pytest.fixture(scope="module")
def scene():
    from backend.ephem import ephemeris
    eph = ephemeris.parse_rinex(FIX)
    rx = np.array(geometry.llh_to_ecef(41.0, 29.0, 100.0))
    sat_pos, pr, wt, el = {}, {}, {}, {}
    for prn, e in eph.items():
        o = geometry.observables(e, rx, TOE)
        if o["el_deg"] < 5:
            continue
        pos, _, tof, clk = geometry.solve_transmit_time(e, rx, TOE)
        sat_pos[prn] = pos
        # satellite-clock corrected pseudorange == pure geometric range
        pr[prn] = float(np.linalg.norm(pos - rx))
        wt[prn] = wls.elevation_weight(o["el_deg"])
        el[prn] = o["el_deg"]
    return rx, sat_pos, pr, wt, el


def test_recovers_truth_from_clean_pseudoranges(scene):
    rx, sat_pos, pr, wt, _ = scene
    sol = wls.solve(pr, sat_pos, weights=wt, x0=[*rx, 0.0])
    assert np.linalg.norm(np.array(sol["ecef"]) - rx) < 1e-3
    assert abs(sol["clock_bias_s"]) < 1e-9
    assert sol["residual_rms_m"] < 1e-3


def test_raises_below_four_sats(scene):
    _, sat_pos, pr, _, _ = scene
    few = dict(list(pr.items())[:3])
    with pytest.raises(ValueError):
        wls.solve(few, sat_pos)


def test_weighting_changes_the_estimate_under_noise(scene):
    rx, sat_pos, pr, wt, el = scene
    rng = np.random.default_rng(0)
    # big blunder on the lowest-elevation sat
    low = min(el, key=el.get)
    noisy = dict(pr)
    noisy[low] += 60.0
    ols = wls.solve(noisy, sat_pos, x0=[*rx, 0.0])
    weighted = wls.solve(noisy, sat_pos, weights=wt, x0=[*rx, 0.0])
    e_ols = np.linalg.norm(np.array(ols["ecef"]) - rx)
    e_w = np.linalg.norm(np.array(weighted["ecef"]) - rx)
    assert e_w < e_ols                     # down-weighting the bad low sat helps


def test_dop_block_is_consistent(scene):
    _, sat_pos, pr, wt, _ = scene
    sol = wls.solve(pr, sat_pos, weights=wt)
    d = sol["dop"]
    assert d["gdop"] > 0
    assert d["pdop"] == pytest.approx(math.sqrt(d["hdop"] ** 2 + d["vdop"] ** 2), rel=1e-6)
    assert d["gdop"] == pytest.approx(math.sqrt(d["pdop"] ** 2 + d["tdop"] ** 2), rel=1e-6)
    # matches the legacy geometry.dop within rounding
    entries = [{"_los": (np.array(sat_pos[p]) - np.array(sol["ecef"]))
                / np.linalg.norm(np.array(sat_pos[p]) - np.array(sol["ecef"]))}
               for p in sol["prns"]]
    for e in entries:
        e["_los"] = e["_los"].tolist()
    legacy = geometry.dop(entries, sol["ecef"])
    assert legacy["pdop"] == pytest.approx(d["pdop"], rel=1e-3)


def test_covariance_grows_with_measurement_sigma(scene):
    rx, sat_pos, pr, _, el = scene
    rng = np.random.default_rng(1)
    noisy = {p: v + rng.normal(0, 2.0) for p, v in pr.items()}
    tight = {p: wls.elevation_weight(el[p], sigma0_m=1.0) for p in pr}
    loose = {p: wls.elevation_weight(el[p], sigma0_m=8.0) for p in pr}
    st = wls.solve(noisy, sat_pos, weights=tight, x0=[*rx, 0.0])
    sl = wls.solve(noisy, sat_pos, weights=loose, x0=[*rx, 0.0])
    assert sl["sigma_horizontal_m"] > st["sigma_horizontal_m"]
    assert st["sigma_vertical_m"] > 0
