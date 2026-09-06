import pytest

from backend.synth import fs_policy, signals


def test_l1_band_floor_unchanged():
    assert fs_policy.band_floor("L1", ["GPS_L1CA"]) == pytest.approx(1.023e6)


def test_g1_band_floor_covers_fdma_span():
    f = fs_policy.band_floor("G1", ["GLO_G1"], ks=range(-7, 7))
    assert f >= 2 * (7 * 562_500 + 511_000)


def test_channel_offset_linear():
    assert signals.glo_channel_offset_hz(0) == 0.0
    assert signals.glo_channel_offset_hz(-7) == -7 * 562_500.0


def test_channel_offset_rejects_out_of_range():
    with pytest.raises(ValueError):
        signals.glo_channel_offset_hz(7)
    with pytest.raises(ValueError):
        signals.glo_channel_offset_hz(-8)


def test_fs_min_default_unchanged():
    result = fs_policy.fs_min(["GPS_L1CA"])
    result_with_zero = fs_policy.fs_min(["GPS_L1CA"], channel_span_hz=0.0)
    assert result == pytest.approx(result_with_zero)
