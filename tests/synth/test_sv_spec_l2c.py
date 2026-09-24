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
        "pseudorange_m": 21_000_000.0,
    }


def _code(spec):
    return np.ctypeslib.as_array(ctypes.cast(
        spec.code, ctypes.POINTER(ctypes.c_int8 * spec.code_len)).contents)


def test_sv_spec_l2c_is_cm_cl_time_multiplexed_at_1023_kcps():
    spec, keep = engine._sv_spec_for(_l2c_entry(), 1.0, nav=None)
    assert spec is not None
    assert spec.code_len == 20460 and spec.chip_rate_hz == 1.023e6
    assert spec.sub_carrier_hz == 0.0 and spec.sec_len == 0
    (cl, _cl_keep), = spec._companions
    assert cl.code_len == 1534500 and cl.chip_rate_hz == 1.023e6
    assert cl.nav_mode == 0
    cm_ref, cl_ref = _lib.code_l2c(1)
    cm = _code(spec)
    assert np.array_equal(cm[0::2], cm_ref) and not cm[1::2].any()
    clc = _code(cl)
    assert np.array_equal(clc[1::2], cl_ref) and not clc[0::2].any()


def test_sv_spec_l2c_attaches_cnav_stream_to_cm_only():
    nav_buf = (ctypes.c_int8 * 100)(*([1, -1] * 50))
    spec, keep = engine._sv_spec_for(_l2c_entry(), 1.0, nav=(nav_buf, 100, 50.0))
    assert spec.nav_mode == 1
    assert spec.nav_nbits == 100
    assert spec.nav_sym_rate_hz == 50.0
    (cl, _k), = spec._companions
    assert cl.nav_mode == 0


def test_l2c_components_scale_geometry_and_align_cl_to_transmit_time():
    e = _l2c_entry()
    spec, _ = engine._sv_spec_for(e, 1.0, nav=None)
    (cl, _k), = spec._companions
    sow = 345_600.0
    for c in (spec, cl):
        engine._finish_component(c, e, sow, None)
    assert spec.code_phase0_chips == 25.0          # 12.5 CM chips -> slots
    assert spec.code_doppler_hz == 1.6
    # CM and CL chip indices at sample 0 (mixer: -code_phase0 mod L) both
    # equal the transmit-time slot count u0 modulo their own period, so CL
    # chip 0 falls on the 1.5 s X1 epoch.
    eff_cm = (20460 - spec.code_phase0_chips % 20460) % 20460
    u0 = eff_cm + spec.tx_chips_offset
    idx_cl = (-cl.code_phase0_chips) % 1534500
    assert abs(idx_cl - u0 % 1534500) < 1e-6
    # u0 is slots since the stream start: (sow - t0) - flight time
    t0 = engine.nav_encoders.stream_t0_sow("G", e["signal_id"],
                                            sow - engine._NAV_LEAD_S)
    exp = ((sow - t0) - e["pseudorange_m"] / engine.config.C) * 1.023e6
    assert abs(u0 - exp) < 1e-3
