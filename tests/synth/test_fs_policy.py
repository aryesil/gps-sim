import pytest

from backend.synth import fs_policy


def test_l2_floor_covers_l2c_chip_rate():
    fs = fs_policy.band_floor("L2", ["GPS_L2C"])
    assert fs >= 1.023e6


def test_l5_floor_covers_1023_mcps():
    fs = fs_policy.band_floor("L5", ["GPS_L5I"])
    assert fs >= 10.23e6
    assert fs >= 20.0e6


def test_g2_floor_uses_437500_hz_channel_step():
    fs = fs_policy.band_floor("G2", ["GLO_L2OF"], ks=range(-7, 7))
    # span = 2 * (7 * 437_500 + 511_000) = 2 * 3_573_500 = 7_147_000
    assert fs >= 7_147_000.0


def test_unknown_band_still_raises():
    with pytest.raises(ValueError):
        fs_policy.band_floor("XYZ", ["GPS_L1CA"])
