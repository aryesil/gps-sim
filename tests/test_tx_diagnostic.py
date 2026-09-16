import itertools

import numpy as np

from backend.rf.frontend.diagnostic import cw_chunk_source


def test_default_tone_is_baseband_dc_and_never_ends():
    gen = cw_chunk_source(sample_rate_hz=2_600_000.0, chunk_samples=1000)
    first_three = list(itertools.islice(gen, 3))
    assert len(first_three) == 3
    for chunk in first_three:
        assert chunk.dtype == np.complex64
        assert chunk.shape == (1000,)
        assert np.allclose(chunk, 1.0 + 0.0j)


def test_nonzero_tone_hz_produces_a_rotating_baseband_tone():
    fs = 2_600_000.0
    tone_hz = 50_000.0
    gen = cw_chunk_source(sample_rate_hz=fs, chunk_samples=8192, tone_hz=tone_hz)
    chunk = next(gen)
    spectrum = np.abs(np.fft.fft(chunk))
    peak_bin = np.argmax(spectrum)
    freqs = np.fft.fftfreq(len(chunk), d=1.0 / fs)
    assert abs(freqs[peak_bin] - tone_hz) < (fs / len(chunk)) * 1.5


def test_consecutive_chunks_are_phase_continuous():
    fs = 2_600_000.0
    tone_hz = 20_000.0
    gen = cw_chunk_source(sample_rate_hz=fs, chunk_samples=1000, tone_hz=tone_hz)
    c1 = next(gen)
    c2 = next(gen)
    stitched = np.concatenate([c1, c2])

    gen_whole = cw_chunk_source(sample_rate_hz=fs, chunk_samples=2000, tone_hz=tone_hz)
    whole = next(gen_whole)
    assert np.allclose(stitched, whole, atol=1e-6)
