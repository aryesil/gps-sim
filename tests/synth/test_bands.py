import pytest

from backend.synth import bands, fs_policy, signals


class _Req:
    sample_rate = 5_000_000.0
    sample_format = "int16"
    g1_sample_rate = None
    l2_sample_rate = None
    l5_sample_rate = None


def _entry(signal_key):
    return {"signal_id": signals.SIGNALS[signal_key],
            "sys": signals.SIGNALS[signal_key].sys}


def test_registry_insertion_order_is_l1_g1_l2_l5():
    assert list(bands.BAND_REGISTRY) == ["L1", "G1", "L2", "L5"]


def test_l1_only_scenario_emits_exactly_gpssim_bin():
    plans = bands.plan_bands([_entry("GPS_L1CA")], _Req())
    assert [p.out_file for p in plans] == ["gpssim.bin"]
    assert plans[0].centre_hz == 1_575_420_000.0


def test_empty_band_is_skipped():
    plans = bands.plan_bands([_entry("GPS_L1CA"), _entry("GLO_G1")], _Req())
    assert [p.id for p in plans] == ["L1", "G1"]


def test_l2_entry_produces_l2_plan_after_l1():
    req = _Req()
    req.l2_sample_rate = 5_000_000.0
    plans = bands.plan_bands([_entry("GPS_L1CA"), _entry("GPS_L2C")], req)
    assert [p.id for p in plans] == ["L1", "L2"]
    assert plans[1].out_file == "gpssim_l2.bin"
    assert plans[1].centre_hz == 1_227_600_000.0


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
