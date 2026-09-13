import numpy as np

from backend.synth import _lib


def _xcorr_peak(a, b):
    A = np.fft.fft(a)
    B = np.fft.fft(b)
    return float(np.max(np.abs(np.fft.ifft(A * np.conj(B)))) / len(a))


def test_e5a_period_and_alphabet():
    ei, eq = _lib.code_e5a(1)
    assert ei.shape == (10230,)
    assert eq.shape == (10230,)
    assert set(np.unique(ei)).issubset({-1, 1})
    assert set(np.unique(eq)).issubset({-1, 1})


def test_e5a_i_and_q_differ():
    ei, eq = _lib.code_e5a(7)
    assert not np.array_equal(ei, eq)


def test_e5a_is_deterministic():
    assert np.array_equal(_lib.code_e5a(11)[0], _lib.code_e5a(11)[0])


def test_e5a_roughly_balanced():
    ei, eq = _lib.code_e5a(3)
    # Real ICD memory codes -- much tighter than a seeded LFSR surrogate.
    assert abs(int(ei.sum())) < 200
    assert abs(int(eq.sum())) < 200


def test_e5a_autocorr_is_a_sharp_peak():
    ei, _ = _lib.code_e5a(5)
    f = ei.astype(np.float64)
    assert _xcorr_peak(f, f) > 0.99


def test_e5a_distinct_prns_decorrelate():
    a = _lib.code_e5a(1)[0].astype(np.float64)
    b = _lib.code_e5a(2)[0].astype(np.float64)
    # Real ICD memory codes: cross-correlation floor is much tighter than
    # the seeded-surrogate L2C/L5 codes.
    assert _xcorr_peak(a, b) < 0.1


def test_e5a_bad_prn_rejected():
    for prn in (0, 51):
        try:
            _lib.code_e5a(prn)
        except ValueError:
            pass
        else:
            raise AssertionError(f"prn {prn} should be rejected")
