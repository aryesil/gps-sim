from backend import config
from backend.synth import signals


def test_l2c_signal_registered_on_l2_band():
    s = signals.SIGNALS["GPS_L2C"]
    assert s.band == "L2"
    assert s.carrier_hz == config.L2_HZ
    assert s.chip_rate_hz == 1.023e6
    assert s.code_len == 10230
    assert s.sys == "G"


def test_l5_signals_on_l5_band_at_1023_mcps():
    for key in ("GPS_L5I", "GPS_L5Q", "GAL_E5AI", "BDS_B2AD"):
        s = signals.SIGNALS[key]
        assert s.band == "L5"
        assert s.carrier_hz == config.L5_HZ
        assert s.chip_rate_hz == 10.23e6


def test_signals_for_filters_by_system_and_band():
    got = signals.signals_for("G", {"L1", "L2"})
    bands = {s.band for s in got}
    assert bands == {"L1", "L2"}
    assert all(s.sys == "G" for s in got)


def test_signals_for_all_bands_default():
    got = signals.signals_for("G")
    assert {s.band for s in got} == {"L1", "L2", "L5"}


def test_signals_for_unknown_system_is_empty():
    assert signals.signals_for("Z") == []


def test_systems_tuple_has_navic():
    assert "I" in signals.SYSTEMS
