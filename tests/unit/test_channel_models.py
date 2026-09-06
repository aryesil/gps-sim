import math
import pathlib

import pytest

from backend.models import channel_models
from backend.ephem import ephemeris
from backend import geometry

_FIX = pathlib.Path(__file__).parents[1] / "fixtures" / "brdc_sample.rnx"


def test_disabled_by_default_is_noop():
    m = channel_models.ChannelModels.from_request({})
    assert not m.any_enabled
    args = m.observable_args(50000.0, 41.0, 29.0, 100.0)
    assert args["atmo_delay_fn"] is None
    assert args["rx_clock_range_m"] == 0.0
    assert args["mp_code_bias_m"] == 0.0
    s = m.summary(50000.0, 41.0, 29.0, 100.0)
    assert s["any_enabled"] is False


def test_atmosphere_klobuchar_and_saastamoinen_add_delay():
    m = channel_models.ChannelModels.from_request(
        {"atmosphere": {"ionosphere": "klobuchar", "troposphere": "saastamoinen"}})
    assert m.any_enabled
    fn = m.observable_args(50400.0, 41.0, 29.0, 100.0)["atmo_delay_fn"]
    d = fn(math.radians(180.0), math.radians(30.0))
    assert d > 0.0
    s = m.summary(50400.0, 41.0, 29.0, 100.0)
    assert s["ionosphere_delay_m"] > 0.0
    assert s["troposphere_delay_m"] > 0.0


def test_receiver_clock_common_range_bias():
    m = channel_models.ChannelModels.from_request(
        {"receiver_clock": {"model": "poly", "bias_s": 1e-6}})
    args = m.observable_args(50400.0, 0.0, 0.0, 0.0)
    assert args["rx_clock_range_m"] == pytest.approx(299792458.0 * 1e-6)


def test_multipath_code_bias_flows_into_observables():
    m = channel_models.ChannelModels.from_request(
        {"multipath": {"model": "specular",
                       "reflections": [{"excess_delay_m": 50.0, "amplitude": 0.5,
                                        "phase_rad": 0.0}]}})
    bias = m.observable_args(50400.0, 0.0, 0.0, 0.0)["mp_code_bias_m"]
    assert bias != 0.0
    eph_by_prn = ephemeris.parse_rinex(_FIX)
    e = eph_by_prn[sorted(eph_by_prn)[0]]
    rx = geometry.llh_to_ecef(41.0, 29.0, 100.0)
    t_rx = e["toe"]
    base = geometry.observables(e, rx, t_rx)
    biased = geometry.observables(e, rx, t_rx, mp_code_bias_m=bias)
    assert biased["pseudorange_m"] - base["pseudorange_m"] == pytest.approx(bias)
    assert biased["multipath_code_bias_m"] == pytest.approx(bias)


def test_invalid_model_raises_valueerror():
    with pytest.raises(ValueError):
        channel_models.ChannelModels.from_request({"multipath": {"model": "rician"}})
