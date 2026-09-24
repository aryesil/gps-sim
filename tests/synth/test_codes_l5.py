import numpy as np

from backend.synth import _lib


def _xcorr_peak(a, b):
    A = np.fft.fft(a)
    B = np.fft.fft(b)
    return float(np.max(np.abs(np.fft.ifft(A * np.conj(B)))) / len(a))


def test_l5_period_and_alphabet():
    i5, q5 = _lib.code_l5(1)
    assert i5.shape == (10230,)
    assert q5.shape == (10230,)
    assert set(np.unique(i5)).issubset({-1, 1})
    assert set(np.unique(q5)).issubset({-1, 1})


def test_l5_i_and_q_differ():
    i5, q5 = _lib.code_l5(7)
    assert not np.array_equal(i5, q5)


def test_l5_is_deterministic():
    assert np.array_equal(_lib.code_l5(11)[0], _lib.code_l5(11)[0])


def test_l5_roughly_balanced():
    i5, _ = _lib.code_l5(3)
    # 10230-chip sequence: |sum| well under a few hundred for a balanced code.
    assert abs(int(i5.sum())) < 600


def test_l5_autocorr_is_a_sharp_peak():
    i5, _ = _lib.code_l5(5)
    f = i5.astype(np.float64)
    assert _xcorr_peak(f, f) > 0.99


def test_l5_icd_known_answer():
    # IS-GPS-705 Table 3-Ia/3-Ib XB advances (PRN 1: I5 266, Q5 1701);
    # chips match GNSS-SDR / PocketSDR.
    i5, q5 = _lib.code_l5(1)
    assert i5[:10].tolist() == [-1, -1, 1, -1, -1, 1, 1, 1, -1, 1]
    assert q5[:10].tolist() == [-1, -1, 1, 1, -1, -1, 1, 1, -1, 1]


def test_l5_distinct_prns_decorrelate():
    a = _lib.code_l5(1)[0].astype(np.float64)
    b = _lib.code_l5(2)[0].astype(np.float64)
    assert _xcorr_peak(a, b) < 0.15


def test_l5_bad_prn_rejected():
    try:
        _lib.code_l5(0)
    except ValueError:
        pass
    else:
        raise AssertionError("prn 0 should be rejected")
