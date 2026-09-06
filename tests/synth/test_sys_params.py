import math
import datetime as dt
import pathlib

from backend import config, ephemeris, geometry
from backend.synth import signals

_RINEX = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_full.rnx")


def test_signal_has_sys_and_subcarrier_defaults():
    s = signals.SIGNALS["GPS_L1CA"]
    assert s.sys == "G"
    assert s.sub_carrier_hz == 0.0
    assert s.carrier_hz == config.L1_HZ
    assert s.chip_rate_hz == config.CA_CHIP_HZ
    assert s.code_len == config.CA_CODE_LEN


def test_observables_signal_none_matches_gps_defaults():
    eph = ephemeris.align_epochs(
        ephemeris.parse_rinex(_RINEX),
        *ephemeris.gps_week_and_sow(dt.datetime(2024, 1, 1, 0, 0, 18)))
    rx = geometry.llh_to_ecef(41.0, 29.0, 100.0)
    prn = sorted(eph)[0]
    t = ephemeris.gps_week_and_sow(dt.datetime(2024, 1, 1, 0, 0, 18))[1] + 100.0
    a = geometry.observables(eph[prn], rx, t)
    b = geometry.observables(eph[prn], rx, t, signal=signals.SIGNALS["GPS_L1CA"])
    assert a["carrier_doppler_hz"] == b["carrier_doppler_hz"]
    assert a["code_phase_chips"] == b["code_phase_chips"]
    assert a["code_doppler_hz"] == b["code_doppler_hz"]


def test_observables_other_signal_scales_code_phase():
    eph = ephemeris.align_epochs(
        ephemeris.parse_rinex(_RINEX),
        *ephemeris.gps_week_and_sow(dt.datetime(2024, 1, 1, 0, 0, 18)))
    rx = geometry.llh_to_ecef(41.0, 29.0, 100.0)
    prn = sorted(eph)[0]
    t = ephemeris.gps_week_and_sow(dt.datetime(2024, 1, 1, 0, 0, 18))[1] + 100.0
    b1i = signals.Signal(config.L1_HZ, 2.046e6, 2046, None, 50.0, "L1", sys="C")
    o = geometry.observables(eph[prn], rx, t, signal=b1i)
    # same carrier -> same carrier Doppler; code phase is now modulo 2046
    assert 0.0 <= o["code_phase_chips"] < 2046.0
    assert math.isclose(o["code_doppler_hz"],
                        o["carrier_doppler_hz"] * 2.046e6 / config.L1_HZ,
                        rel_tol=1e-9)
