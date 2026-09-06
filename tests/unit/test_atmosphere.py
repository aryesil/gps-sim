"""Klobuchar ionosphere and Saastamoinen troposphere models."""
import math

import pytest

from backend.models import atmosphere
from backend.models.atmosphere import (AtmosphereConfig, delays_for_los,
                                klobuchar_delay_m, saastamoinen_delay_m)

ALPHA = atmosphere.DEFAULT_KLOBUCHAR_ALPHA
BETA = atmosphere.DEFAULT_KLOBUCHAR_BETA


# ---- config -------------------------------------------------------------

def test_default_config_is_disabled():
    cfg = AtmosphereConfig()
    assert not cfg.enabled
    assert AtmosphereConfig.from_dict(None).ionosphere == "off"


def test_from_dict_rejects_model_on_wrong_layer():
    with pytest.raises(ValueError):
        AtmosphereConfig.from_dict({"ionosphere": "saastamoinen"})
    with pytest.raises(ValueError):
        AtmosphereConfig.from_dict({"troposphere": "klobuchar"})
    with pytest.raises(ValueError):
        AtmosphereConfig.from_dict({"ionosphere": "nequick"})


# ---- Klobuchar --------------------------------------------------------

def test_klobuchar_zenith_delay_is_a_few_metres_and_positive():
    d = klobuchar_delay_m(ALPHA, BETA, 50400.0, math.radians(40), math.radians(30),
                          az_rad=0.0, el_rad=math.radians(90))
    assert 0.5 < d["delay_m"] < 20.0
    assert d["slant_factor"] == pytest.approx(1.0, abs=0.05)


def test_klobuchar_low_elevation_delay_exceeds_zenith():
    zen = klobuchar_delay_m(ALPHA, BETA, 50400.0, math.radians(40), math.radians(30),
                            0.0, math.radians(85))["delay_m"]
    low = klobuchar_delay_m(ALPHA, BETA, 50400.0, math.radians(40), math.radians(30),
                            0.0, math.radians(10))["delay_m"]
    assert low > 2.0 * zen


def test_klobuchar_night_delay_floor():
    # local time far from the 14:00 peak -> the 5 ns night value * slant
    d = klobuchar_delay_m(ALPHA, BETA, 0.0, math.radians(40), 0.0,
                          0.0, math.radians(90))
    # night vertical delay is the 5 ns floor; slant ~1.0004 at zenith
    assert d["vertical_delay_m"] == pytest.approx(5e-9 * 299792458.0, rel=1e-6)
    assert d["delay_m"] == pytest.approx(d["vertical_delay_m"] * d["slant_factor"], rel=1e-9)


def test_klobuchar_handles_zero_coefficients():
    d = klobuchar_delay_m((0, 0, 0, 0), (0, 0, 0, 0), 50400.0,
                          math.radians(40), 0.0, 0.0, math.radians(45))
    assert d["delay_m"] > 0.0                        # still the night floor, finite
    assert math.isfinite(d["delay_m"])


def test_klobuchar_is_continuous_in_elevation():
    prev = None
    for el_deg in range(85, 5, -1):
        d = klobuchar_delay_m(ALPHA, BETA, 50400.0, math.radians(40),
                              math.radians(30), math.radians(45), math.radians(el_deg))["delay_m"]
        if prev is not None:
            assert abs(d - prev) < 1.5             # no jumps
        prev = d


# ---- Saastamoinen ---------------------------------------------------

def test_saastamoinen_zenith_delay_near_2_3_m_at_sea_level():
    d = saastamoinen_delay_m(math.radians(90), 0.0)
    assert 2.2 < d["delay_m"] < 2.6
    assert d["zenith_hydrostatic_m"] > d["zenith_wet_m"]


def test_saastamoinen_grows_with_lower_elevation():
    d90 = saastamoinen_delay_m(math.radians(90), 0.0)["delay_m"]
    d30 = saastamoinen_delay_m(math.radians(30), 0.0)["delay_m"]
    d10 = saastamoinen_delay_m(math.radians(10), 0.0)["delay_m"]
    assert d10 > d30 > d90
    assert d30 == pytest.approx(2.0 * d90, rel=0.05)     # 1/sin(30) = 2


def test_saastamoinen_decreases_with_altitude():
    low = saastamoinen_delay_m(math.radians(90), 0.0)["delay_m"]
    high = saastamoinen_delay_m(math.radians(90), 3000.0)["delay_m"]
    assert high < low


# ---- combiner: applied exactly once ------------------------------

def test_delays_for_los_off_is_zero():
    out = delays_for_los(AtmosphereConfig(), 50400.0, 0.7, 0.5, 100.0, 0.0, 1.0)
    assert out["total_m"] == 0.0
    assert out["ionosphere"] is None and out["troposphere"] is None


def test_delays_for_los_sums_each_model_once():
    cfg = AtmosphereConfig(ionosphere="klobuchar", troposphere="saastamoinen")
    args = (50400.0, math.radians(40), math.radians(30), 120.0,
            math.radians(45), math.radians(25))
    out = delays_for_los(cfg, *args)
    iono = out["ionosphere"]["delay_m"]
    tropo = out["troposphere"]["delay_m"]
    assert out["total_m"] == pytest.approx(iono + tropo, rel=1e-12)
    assert iono > 0 and tropo > 0
