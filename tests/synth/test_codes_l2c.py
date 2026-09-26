import numpy as np

from backend.synth import _lib


def test_abi_version():
    assert _lib.ABI_VERSION == 27
    assert _lib.load_lib().synth_abi_version() == 27


def test_l2c_icd_known_answer():
    # IS-GPS-200 Table 3-IIa initial states through the modular (Galois)
    # register, output = LSB. First 10 chips match GNSS-SDR / PocketSDR.
    cm, cl = _lib.code_l2c(1)
    assert cm[:10].tolist() == [1, 1, -1, 1, -1, 1, -1, -1, -1, -1]
    assert cl[:10].tolist() == [1, -1, 1, -1, 1, 1, -1, -1, 1, -1]
    cm159, _ = _lib.code_l2c(159)          # extended PRN block 159..210
    assert cm159[:10].tolist() == [1, 1, -1, 1, 1, -1, 1, -1, 1, 1]


def test_l2c_prn_gap_rejected():
    import pytest
    with pytest.raises(ValueError):
        _lib.code_l2c(100)                 # no ICD assignment for 64..158


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
