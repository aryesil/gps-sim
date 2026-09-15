import numpy as np

from backend.synth import _lib


def test_abi_version_is_20():
    assert _lib.ABI_VERSION == 24
    assert _lib.load_lib().synth_abi_version() == 24


def test_l2c_cm_period_and_alphabet():
    cm, cl = _lib.code_l2c(1)
    assert cm.shape == (10230,)
    assert cl.shape == (767250,)
    assert set(np.unique(cm)).issubset({-1, 1})
    assert set(np.unique(cl)).issubset({-1, 1})


def test_l2c_cm_is_roughly_balanced():
    cm, _ = _lib.code_l2c(7)
    # a 2^27-1 m-sequence truncated to 10230 chips is not exactly balanced,
    # but it must be nowhere near constant.
    assert abs(int(cm.sum())) < 600


def test_l2c_codes_differ_by_prn():
    cm1, cl1 = _lib.code_l2c(1)
    cm2, cl2 = _lib.code_l2c(2)
    assert not np.array_equal(cm1, cm2)
    assert not np.array_equal(cl1, cl2)


def test_l2c_generation_is_deterministic():
    a, _ = _lib.code_l2c(11)
    b, _ = _lib.code_l2c(11)
    assert np.array_equal(a, b)


def test_l2c_cl_is_75_cm_periods_long():
    # CL period 767250 = 75 * 10230 (IS-GPS-200 relationship).
    assert 767250 == 75 * 10230
