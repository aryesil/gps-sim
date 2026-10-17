import importlib.util
import pathlib

import numpy as np

_p = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cs8_tx_selftest.py"
_spec = importlib.util.spec_from_file_location("cs8_tx_selftest", _p)
st = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(st)

RATE = 4e6
N = 65536
TONE = 8192 * (RATE / N)


def _cap(q_gain=1.0):
    t = np.arange(N) / RATE
    rng = np.random.default_rng(1)
    i = np.cos(2 * np.pi * TONE * t)
    q = q_gain * np.sin(2 * np.pi * TONE * t)
    return (100 * (i + 1j * q) + rng.normal(0, 0.3, N)).astype(np.complex64)


def test_clean_tone_passes():
    r = st.analyze(_cap(), RATE, TONE)
    assert abs(r["peak_hz"] - TONE) < 1
    assert r["image_rejection_db"] > 40
    assert st.verdict(r, RATE)


def test_dead_q_path_is_detected():
    # Q stuck at 0: a real cosine, equal tones at +f and -f
    r = st.analyze(_cap(q_gain=0.0), RATE, TONE)
    assert r["image_rejection_db"] < 3
    assert not st.verdict(r, RATE)


def test_swapped_iq_reports_negative_frequency():
    r = st.analyze(np.conj(_cap()), RATE, TONE)
    assert r["peak_hz"] < 0 and not st.verdict(r, RATE)
