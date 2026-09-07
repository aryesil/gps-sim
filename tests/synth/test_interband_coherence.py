"""Phase 0 acceptance: L1 / L2 / L5 share one satellite state; the band
difference is only the carrier-frequency scaling of Doppler, the 1/f^2
dispersion of the ionosphere, and the Tgd/ISC group-delay differential."""
import pathlib

import pytest

from backend import config, geometry
from backend.ephem import ephemeris
from backend.models import atmosphere
from backend.synth import signals

_FIX = pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_sample.rnx"
_RX_LLH = (41.0082, 28.9784, 100.0)
_T_RX = 475200.0
_F_L1 = 154 * 10.23e6
_F_L2 = 120 * 10.23e6
_F_L5 = 115 * 10.23e6


def _top_prn(eph, rx):
    return max(geometry.constellation(eph, rx, _T_RX),
              key=lambda e: e["el_deg"])["prn"]


def _obs(sig_key):
    eph = ephemeris.parse_rinex(_FIX)
    rx = geometry.llh_to_ecef(*_RX_LLH)
    prn = _top_prn(eph, rx)
    return geometry.observables(eph[prn], rx, _T_RX,
                                signal=signals.SIGNALS[sig_key])


def test_carrier_doppler_scales_with_band_frequency():
    o1 = _obs("GPS_L1CA")
    o2 = _obs("GPS_L2C")
    o5 = _obs("GPS_L5I")
    assert o2["carrier_doppler_hz"] / o1["carrier_doppler_hz"] == pytest.approx(
        _F_L2 / _F_L1, rel=1e-9)
    assert o5["carrier_doppler_hz"] / o1["carrier_doppler_hz"] == pytest.approx(
        _F_L5 / _F_L1, rel=1e-9)


def test_code_doppler_is_range_rate_scaled_not_carrier_scaled():
    o1 = _obs("GPS_L1CA")
    o5 = _obs("GPS_L5I")
    # code Doppler = range_rate/c * chip_hz; L5 chip rate is 10x L1
    assert o5["code_doppler_hz"] / o1["code_doppler_hz"] == pytest.approx(
        10.0, rel=1e-9)


def test_ionospheric_delay_is_dispersive_by_freq_squared():
    a = atmosphere.DEFAULT_KLOBUCHAR_ALPHA
    b = atmosphere.DEFAULT_KLOBUCHAR_BETA
    args = (a, b, 345600.0, 0.71, 0.51, 1.0, 0.6)
    d1 = atmosphere.klobuchar_delay_m(*args, freq_hz=config.L1_HZ)["delay_m"]
    d2 = atmosphere.klobuchar_delay_m(*args, freq_hz=config.L2_HZ)["delay_m"]
    assert d2 / d1 == pytest.approx((_F_L1 / _F_L2) ** 2, rel=1e-9)


def test_group_delay_differential_matches_is_gps_200():
    eph = ephemeris.parse_rinex(_FIX)
    rx = geometry.llh_to_ecef(*_RX_LLH)
    prn = _top_prn(eph, rx)
    tgd, isc_l2c = 5e-9, -1e-9
    bias = -tgd * (_F_L1 / _F_L2) ** 2 + isc_l2c
    base = geometry.observables(eph[prn], rx, _T_RX,
                                signal=signals.SIGNALS["GPS_L2C"])
    biased = geometry.observables(eph[prn], rx, _T_RX,
                                  signal=signals.SIGNALS["GPS_L2C"],
                                  sv_clock_bias_s=bias)
    assert biased["pseudorange_m"] - base["pseudorange_m"] == pytest.approx(
        -config.C * bias, abs=1e-6)
