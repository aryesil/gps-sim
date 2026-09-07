import ctypes

import numpy as np

from backend.synth import _lib, engine, signals


def _l2c_entry():
    return {
        "signal_id": signals.SIGNALS["GPS_L2C"],
        "sys": "G", "prn": 1,
        "carrier_doppler_hz": 1234.0,
        "code_doppler_hz": 0.8,
        "code_phase_chips": 12.5,
    }


def test_sv_spec_l2c_uses_cm_code_at_1023_mcps():
    spec, keep = engine._sv_spec_for(_l2c_entry(), 1.0, nav=None)
    assert spec is not None
    assert spec.code_len == 10230
    assert spec.chip_rate_hz == 1.023e6
    assert spec.sub_carrier_hz == 0.0
    assert spec.sec_len == 0
    cm, _cl = _lib.code_l2c(1)
    got = np.ctypeslib.as_array(
        ctypes.cast(spec.code, ctypes.POINTER(ctypes.c_int8 * 10230)).contents)
    assert np.array_equal(got, cm)


def test_sv_spec_l2c_attaches_cnav_stream_at_50hz():
    nav_buf = (ctypes.c_int8 * 100)(*([1, -1] * 50))
    spec, keep = engine._sv_spec_for(_l2c_entry(), 1.0, nav=(nav_buf, 100, 50.0))
    assert spec.nav_mode == 1
    assert spec.nav_nbits == 100
    assert spec.nav_sym_rate_hz == 50.0
