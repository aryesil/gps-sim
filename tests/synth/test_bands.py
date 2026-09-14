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


def test_g2_centre_is_glonass_own_l2_fdma_plan_not_gps_l2c():
    """1246.00 MHz (GLONASS ICD L1/L2, k=0), ~18.75 MHz from GPS L2C's
    1227.60 MHz -- same separate-carrier relationship G1 (1602.00) has to
    GPS L1 (1575.42), not a shared centre."""
    reg = bands.full_band_registry()
    assert reg["G2"].centre_hz == 1_246_000_000.0
    assert reg["G2"].out_file == "gpssim_g2.bin"
    assert signals.SIGNALS["GLO_L2OF"].carrier_hz == 1_246_000_000.0


def test_glo_channel_offset_step_hz_defaults_to_g1_step():
    assert signals.glo_channel_offset_hz(1) == 562_500.0


def test_glo_channel_offset_step_hz_overridable_for_g2():
    assert signals.glo_channel_offset_hz(1, step_hz=437_500.0) == 437_500.0
    assert signals.glo_channel_offset_hz(-7, step_hz=437_500.0) == -7 * 437_500.0


def test_signals_for_l2_pulls_in_glonass_l2of_via_alias():
    """L2OF has no bands-unset default (that's G1's slot -- signal_for("R")
    is unchanged, still GLO_G1), so an explicit bands=["L2"] request is the
    only way to reach it; without the alias it would resolve nothing for R
    at all."""
    assert signals.signals_for("R", ("L2",)) == [signals.SIGNALS["GLO_L2OF"]]
    assert signals.signal_for("R") == signals.SIGNALS["GLO_G1"]


def test_signals_for_l1_still_drops_glonass_no_alias_regression():
    """Existing, tested contract (app._internal_bands_for): explicit
    bands=["L1"] contributes nothing for GLONASS -- only G1's own-default
    path does. The new L2->G2 alias must not also alias L1->G1."""
    assert signals.signals_for("R", ("L1",)) == []
