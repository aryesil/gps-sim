"""Deterministic receiver-clock error model."""
import math

import pytest

from backend.models import receiver_clock
from backend.models.receiver_clock import ReceiverClockConfig, offset_s, state

_C = 299792458.0
_L1 = 1_575_420_000.0


def test_default_is_disabled_and_zero():
    cfg = ReceiverClockConfig()
    assert not cfg.enabled
    assert offset_s(cfg, 123456.0) == 0.0
    s = state(cfg, 123456.0)
    assert s["clock_offset_s"] == 0.0 and s["range_bias_m"] == 0.0
    assert s["carrier_offset_hz"] == 0.0


def test_from_dict_rejects_unknown_model_and_negative_sawtooth():
    with pytest.raises(ValueError):
        ReceiverClockConfig.from_dict({"model": "kalman"})
    with pytest.raises(ValueError):
        ReceiverClockConfig.from_dict({"model": "poly", "sawtooth_amp_s": -1.0})


def test_constant_bias_only():
    cfg = ReceiverClockConfig(model="poly", bias_s=1e-6)
    assert offset_s(cfg, 0.0) == pytest.approx(1e-6)
    assert offset_s(cfg, 9999.0) == pytest.approx(1e-6)
    assert state(cfg, 0.0)["range_bias_m"] == pytest.approx(_C * 1e-6)


def test_linear_drift_is_deterministic_and_linear():
    cfg = ReceiverClockConfig(model="poly", bias_s=0.0, drift_s_per_s=2e-9,
                              ref_epoch_s=100.0)
    a = offset_s(cfg, 110.0)
    b = offset_s(cfg, 120.0)
    assert a == pytest.approx(2e-9 * 10)
    assert b == pytest.approx(2e-9 * 20)
    assert offset_s(cfg, 110.0) == a                 # repeatable, no RNG
    # carrier offset from drift
    assert state(cfg, 110.0)["carrier_offset_hz"] == pytest.approx(-_L1 * 2e-9)


def test_quadratic_term():
    cfg = ReceiverClockConfig(model="poly", drift_rate_s_per_s2=1e-12)
    assert offset_s(cfg, 100.0) == pytest.approx(0.5 * 1e-12 * 100.0 ** 2)


def test_sawtooth_keeps_offset_bounded():
    amp = 1e-6
    cfg = ReceiverClockConfig(model="poly", drift_s_per_s=1e-9,
                              sawtooth_amp_s=amp, sawtooth_period_s=1000.0)
    vals = [offset_s(cfg, t) for t in range(0, 10_000, 25)]
    assert max(abs(v) for v in vals) < 1.5 * amp    # stays roughly within a step
    # without the sawtooth the drift would reach 1e-9 * 10000 = 1e-5
    plain = ReceiverClockConfig(model="poly", drift_s_per_s=1e-9)
    assert offset_s(plain, 9975.0) == pytest.approx(1e-9 * 9975.0)


def test_sawtooth_step_is_one_amp_per_period():
    cfg = ReceiverClockConfig(model="poly", drift_s_per_s=0.0,
                              sawtooth_amp_s=2e-7, sawtooth_period_s=500.0)
    before = offset_s(cfg, 499.0)
    after = offset_s(cfg, 501.0)
    assert before - after == pytest.approx(2e-7)
