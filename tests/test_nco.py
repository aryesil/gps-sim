import numpy as np

from backend.rf.frontend.nco import NCOMixer


def test_mixing_a_dc_input_places_the_tone_at_the_offset_frequency():
    fs = 2_600_000.0
    f_offset = 100_000.0
    n = 8192
    mixer = NCOMixer(f_offset, fs)
    chunk = np.ones(n, dtype=np.complex64)
    out = mixer.mix(chunk)
    spectrum = np.abs(np.fft.fft(out))
    peak_bin = np.argmax(spectrum)
    freqs = np.fft.fftfreq(n, d=1.0 / fs)
    assert abs(freqs[peak_bin] - f_offset) < (fs / n) * 1.5


def test_zero_offset_is_a_no_op():
    fs = 2_600_000.0
    mixer = NCOMixer(0.0, fs)
    chunk = (np.random.default_rng(0).standard_normal(1000)
             + 1j * np.random.default_rng(1).standard_normal(1000)).astype(np.complex64)
    out = mixer.mix(chunk)
    assert np.allclose(out, chunk, atol=1e-5)


def test_phase_is_continuous_across_consecutive_chunks():
    fs = 2_600_000.0
    f_offset = 250_000.0
    n1, n2 = 4000, 3000
    chunk = np.ones(n1 + n2, dtype=np.complex64)

    one_shot = NCOMixer(f_offset, fs).mix(chunk)

    split = NCOMixer(f_offset, fs)
    part1 = split.mix(chunk[:n1])
    part2 = split.mix(chunk[n1:])
    split_result = np.concatenate([part1, part2])

    assert np.allclose(one_shot, split_result, atol=1e-6)


def test_output_dtype_matches_input_dtype():
    mixer = NCOMixer(1_000.0, 2_600_000.0)
    chunk = np.ones(100, dtype=np.complex64)
    out = mixer.mix(chunk)
    assert out.dtype == np.complex64


def test_many_small_chunks_stay_phase_accurate_over_a_long_run():
    # Exercises the exact-integer sample counter over many calls -- the
    # failure mode this design avoids is float phase-accumulation drift.
    fs = 2_600_000.0
    f_offset = 37_000.0
    chunk_size = 500
    n_chunks = 2000  # 1,000,000 samples total
    full = np.ones(chunk_size * n_chunks, dtype=np.complex64)
    reference = NCOMixer(f_offset, fs).mix(full)

    mixer = NCOMixer(f_offset, fs)
    pieces = [mixer.mix(full[i * chunk_size:(i + 1) * chunk_size]) for i in range(n_chunks)]
    stitched = np.concatenate(pieces)
    assert np.allclose(reference, stitched, atol=1e-6)
