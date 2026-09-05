"""Per-satellite pseudorange error budget."""
import math

import pytest

from backend import error_budget


def test_nominal_budget_uses_defaults_and_rss():
    b = error_budget.budget_for_prn(7, elevation_deg=40.0)
    c = b["components"]
    assert c["ionosphere"]["sigma_m"] == 4.0 and not c["ionosphere"]["modelled"]
    assert c["orbit"]["modelled"] is False
    expect = math.sqrt(0.8**2 + 0.3**2 + 4.0**2 + 0.2**2 + 0.5**2 + 0.5**2 + 0.0**2)
    assert b["total_m"] == pytest.approx(expect)


def test_modelled_iono_shrinks_the_term_and_the_total():
    nominal = error_budget.budget_for_prn(7, elevation_deg=40.0)
    modelled = error_budget.budget_for_prn(7, elevation_deg=40.0, iono_delay_m=9.0)
    assert modelled["components"]["ionosphere"]["modelled"] is True
    assert modelled["components"]["ionosphere"]["sigma_m"] == pytest.approx(0.9)
    assert modelled["total_m"] < nominal["total_m"]


def test_multipath_bias_enters_as_full_sigma():
    b = error_budget.budget_for_prn(3, multipath_bias_m=1.2)
    assert b["components"]["multipath"]["sigma_m"] == pytest.approx(1.2)
    assert b["components"]["multipath"]["modelled"] is True


def test_receiver_clock_common_is_reported_but_excluded_from_rss():
    b = error_budget.budget_for_prn(1, receiver_clock_bias_m=300.0)
    rc = b["components"]["receiver_clock_common"]
    assert rc["in_rss"] is False and rc["sigma_m"] == 300.0
    without = error_budget.budget_for_prn(1)
    assert b["total_m"] == pytest.approx(without["total_m"])


def test_overrides_replace_nominal():
    b = error_budget.budget_for_prn(1, overrides={"orbit": 0.1})
    assert b["components"]["orbit"]["sigma_m"] == 0.1


def test_summarize_rolls_up():
    per = [error_budget.budget_for_prn(p, elevation_deg=30.0 + p) for p in (1, 5, 9, 13)]
    s = error_budget.summarize(per)
    assert s["n_sats"] == 4
    assert s["uere_rms_m"] == pytest.approx(
        math.sqrt(sum(b["total_m"] ** 2 for b in per) / 4))
    assert s["uere_max_m"] == max(b["total_m"] for b in per)


def test_summarize_empty():
    s = error_budget.summarize([])
    assert s["n_sats"] == 0 and s["uere_rms_m"] is None
