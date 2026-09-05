import math

from backend import config
from backend.synth import _lib


def test_native_constants_match_python():
    n = _lib.native_constants()
    assert n["l1_hz"] == config.L1_HZ
    assert n["ca_chip_hz"] == config.CA_CHIP_HZ
    assert n["ca_code_len"] == config.CA_CODE_LEN
    assert n["nav_bit_hz"] == config.NAV_BIT_HZ
    assert math.isclose(n["mu"], config.MU, rel_tol=0, abs_tol=0)
    assert math.isclose(n["omega_e_dot"], config.OMEGA_E_DOT, rel_tol=0, abs_tol=0)
    assert n["c"] == config.C
    assert math.isclose(n["f_rel"], config.F_REL, rel_tol=0, abs_tol=0)
    assert n["gps_utc_leap"] == config.GPS_UTC_LEAP_S
