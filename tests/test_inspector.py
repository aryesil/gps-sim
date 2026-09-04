import numpy as np
import pytest

from backend import inspector, config


def test_ca_code_length_and_alphabet():
    c = inspector.ca_code(1)
    assert c.shape == (1023,)
    assert set(np.unique(c).tolist()) <= {-1, 1}


def test_ca_code_autocorrelation_peak():
    c = inspector.ca_code(5).astype(float)
    ac = np.correlate(c, np.concatenate([c, c]), "valid")[:1023]
    assert ac[0] == 1023
    assert np.max(np.abs(ac[1:])) <= 65


def test_ca_codes_are_distinct():
    assert not np.array_equal(inspector.ca_code(1), inspector.ca_code(2))


def test_read_iq_int16_roundtrip(tmp_path):
    raw = np.array([1, -2, 3, -4], dtype=np.int16)
    p = tmp_path / "x.bin"
    raw.tofile(p)
    iq = inspector.read_iq(p, "int16")
    assert np.allclose(iq, [1 - 2j, 3 - 4j])


def test_acquire_finds_synthetic_signal():
    fs = 2.6e6
    prn = 3
    code = inspector.ca_code(prn).astype(float)
    n = int(fs * 0.010)
    t = np.arange(n) / fs
    chips = (t * config.CA_CHIP_HZ + 137.0).astype(int) % 1023
    fd = 1500.0
    sig = code[chips] * np.exp(1j * 2 * np.pi * fd * t)
    rng = np.random.default_rng(0)
    sig = sig + 8 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    res = inspector.acquire(sig.astype(np.complex64), fs, prn)
    assert abs(res["doppler_hz"] - fd) <= 250
    assert abs(res["code_phase_chips"] - 137.0) <= 0.5
    assert res["metric_db"] > 8
