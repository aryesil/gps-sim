"""Deterministic specular-multipath channel model."""
import math

import pytest

from backend import multipath
from backend.multipath import MultipathConfig, Reflection, channel_taps, tracking_bias

_C = 299792458.0
_CHIP_M = _C / 1_023_000.0


def test_default_disabled():
    cfg = MultipathConfig()
    assert not cfg.enabled
    assert channel_taps(cfg) == [(0.0, 1 + 0j)]
    tb = tracking_bias(cfg)
    assert tb["code_bias_m"] == 0.0 and tb["carrier_bias_rad"] == 0.0
    assert tb["n_reflections"] == 0


def test_from_dict_validates():
    with pytest.raises(ValueError):
        MultipathConfig.from_dict({"model": "rician"})
    with pytest.raises(ValueError):
        MultipathConfig.from_dict({"model": "specular",
                                   "reflections": [{"excess_delay_m": -1, "amplitude": 0.3}]})
    with pytest.raises(ValueError):
        MultipathConfig.from_dict({"model": "specular",
                                   "reflections": [{"excess_delay_m": 10, "amplitude": 1.4}]})
    cfg = MultipathConfig.from_dict({"model": "specular", "reflections": [
        {"excess_delay_m": 20.0, "amplitude": 0.5}]})
    assert cfg.enabled and cfg.reflections[0].phase_rad == math.pi


def test_specular_off_when_no_reflections():
    assert not MultipathConfig(model="specular", reflections=[]).enabled


def test_channel_taps_has_direct_plus_one_per_reflection():
    cfg = MultipathConfig(model="specular", reflections=[
        Reflection(30.0, 0.4, phase_rad=math.pi),
        Reflection(90.0, 0.2, phase_rad=0.0)])
    taps = channel_taps(cfg)
    assert len(taps) == 3
    assert taps[0] == (0.0, 1 + 0j)
    assert taps[1][0] == pytest.approx(30.0 / _C)
    assert taps[1][1].real == pytest.approx(-0.4, abs=1e-9)   # phase pi
    assert taps[2][1].real == pytest.approx(0.2, abs=1e-9)    # phase 0


def test_channel_is_time_varying_with_doppler():
    cfg = MultipathConfig(model="specular", reflections=[
        Reflection(30.0, 0.4, phase_rad=0.0, doppler_hz=5.0)])
    g0 = channel_taps(cfg, 0.0)[1][1]
    g1 = channel_taps(cfg, 0.05)[1][1]      # quarter cycle at 5 Hz
    assert abs(g0 - g1) > 0.1
    assert abs(g0) == pytest.approx(abs(g1), rel=1e-9)   # magnitude preserved


def test_tracking_bias_sign_follows_carrier_phase():
    near = MultipathConfig(model="specular", reflections=[Reflection(30.0, 0.5, phase_rad=0.0)])
    far = MultipathConfig(model="specular", reflections=[Reflection(30.0, 0.5, phase_rad=math.pi)])
    bn = tracking_bias(near)["code_bias_m"]
    bf = tracking_bias(far)["code_bias_m"]
    assert bn > 0 > bf                       # in-phase pulls late, anti-phase pulls early
    assert abs(bn) < _CHIP_M                  # bounded by a chip


def test_tracking_bias_grows_with_amplitude_and_is_deterministic():
    small = MultipathConfig(model="specular", reflections=[Reflection(40.0, 0.1, phase_rad=0.0)])
    big = MultipathConfig(model="specular", reflections=[Reflection(40.0, 0.6, phase_rad=0.0)])
    assert tracking_bias(big)["code_bias_m"] > tracking_bias(small)["code_bias_m"]
    assert tracking_bias(big) == tracking_bias(big)


def test_long_delay_reflection_rolls_off():
    short = MultipathConfig(model="specular", reflections=[Reflection(0.5 * _CHIP_M, 0.5, phase_rad=math.pi / 2)])
    long = MultipathConfig(model="specular", reflections=[Reflection(1.9 * _CHIP_M, 0.5, phase_rad=math.pi / 2)])
    assert abs(tracking_bias(long)["carrier_bias_rad"]) < abs(tracking_bias(short)["carrier_bias_rad"])
