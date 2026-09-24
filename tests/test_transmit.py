import sys
import threading
import time
import types

import numpy as np
import pytest

from backend.rf import dual_tx, transmit
from backend import config


def _iq_file(tmp_path, samples=52000, fmt="int16"):
    dtype = np.int8 if fmt == "int8" else np.int16
    data = (np.random.default_rng(0).integers(-100, 100, samples * 2)).astype(dtype)
    p = tmp_path / "g.bin"
    data.tofile(p)
    return str(p)


def test_disabled_without_allow_tx(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_TX", False)
    p = transmit.TxParams(iq_path=_iq_file(tmp_path), sample_rate=2.6e6, sample_format="int16")
    with pytest.raises(transmit.TransmitDisabled):
        transmit.stream(p)


def test_dry_run_paces_and_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_TX", True)
    p = transmit.TxParams(iq_path=_iq_file(tmp_path, samples=52000),
                          sample_rate=2.6e6, sample_format="int16",
                          chunk_samples=13000)
    t0 = time.monotonic()
    out = transmit.stream(p, dry_run=True)
    dt = time.monotonic() - t0
    assert out["dry_run"] is True
    assert out["samples"] == 52000
    assert out["underflow"] == 0
    assert out["elapsed_s"] == pytest.approx(0.02, abs=0.01)
    assert dt >= 0.015


def _recording_sink(seen_chunks):
    real_dry_sink = transmit._DrySink
    class _RecordingSink(real_dry_sink):
        def push(self, chunk):
            seen_chunks.append(chunk)
            super().push(chunk)
    return _RecordingSink


def test_tx_params_kind_defaults_to_pluto(tmp_path):
    p = transmit.TxParams(iq_path=_iq_file(tmp_path), sample_rate=2.6e6,
                          sample_format="int16")
    assert p.kind == "pluto"


def test_default_levels_stream_to_dac_full_scale(tmp_path, monkeypatch):
    # pyadi-iio writes raw int16 words and the AD9361 DAC takes their top 12
    # bits, so a file's own amplitude (gps-sdr-sim ~+-1331, int8 +-127) is
    # NOT the DAC level. Default: RMS leveled to level_dbfs below the
    # backend's full scale, one fixed gain for the whole stream.
    monkeypatch.setattr(config, "ALLOW_TX", True)
    p = transmit.TxParams(iq_path=_iq_file(tmp_path, samples=1000),
                          sample_rate=2.6e6, sample_format="int16",
                          chunk_samples=1000)
    assert p.tx_scale == 1.0 and p.level_dbfs == -15.0
    seen_chunks = []
    monkeypatch.setattr(transmit, "_DrySink", _recording_sink(seen_chunks))
    res = transmit.stream(p, dry_run=True)
    raw = next(transmit._iter_chunks(p.iq_path, p.sample_format, p.chunk_samples))
    target = 32767 * 10 ** (-15 / 20)
    assert abs(transmit._rms(seen_chunks[0]) / target - 1) < 1e-6
    assert np.allclose(seen_chunks[0], raw * res["digital_scale"])
    assert res["clipped"] == 0


def test_int8_and_int16_files_reach_the_same_dac_level(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_TX", True)
    levels = []
    for fmt in ("int8", "int16"):
        d = tmp_path / fmt
        d.mkdir()
        p = transmit.TxParams(iq_path=_iq_file(d, samples=1000, fmt=fmt),
                              sample_rate=2.6e6, sample_format=fmt,
                              chunk_samples=1000)
        seen = []
        monkeypatch.setattr(transmit, "_DrySink", _recording_sink(seen))
        transmit.stream(p, dry_run=True)
        levels.append(transmit._rms(seen[0]))
    assert abs(levels[0] / levels[1] - 1) < 1e-6


def test_bladerf_levels_against_sc16q11(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_TX", True)
    p = transmit.TxParams(iq_path=_iq_file(tmp_path, samples=1000),
                          sample_rate=2.6e6, sample_format="int16",
                          chunk_samples=1000, kind="bladerf")
    seen = []
    monkeypatch.setattr(transmit, "_DrySink", _recording_sink(seen))
    transmit.stream(p, dry_run=True)
    assert abs(transmit._rms(seen[0]) / (2047 * 10 ** (-15 / 20)) - 1) < 1e-6


def test_level_none_is_raw_passthrough(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_TX", True)
    p = transmit.TxParams(iq_path=_iq_file(tmp_path, samples=1000),
                          sample_rate=2.6e6, sample_format="int16",
                          chunk_samples=1000, level_dbfs=None)
    seen_chunks = []
    monkeypatch.setattr(transmit, "_DrySink", _recording_sink(seen_chunks))
    transmit.stream(p, dry_run=True)
    raw = next(transmit._iter_chunks(p.iq_path, p.sample_format, p.chunk_samples))
    assert np.array_equal(seen_chunks[0], raw)


def test_explicit_tx_scale_attenuates_chunks(tmp_path, monkeypatch):
    # The knob multiplies on top of the leveled signal.
    monkeypatch.setattr(config, "ALLOW_TX", True)
    out = []
    for sc in (1.0, 0.25):
        p = transmit.TxParams(iq_path=_iq_file(tmp_path, samples=1000),
                              sample_rate=2.6e6, sample_format="int16",
                              chunk_samples=1000, tx_scale=sc)
        seen_chunks = []
        monkeypatch.setattr(transmit, "_DrySink", _recording_sink(seen_chunks))
        transmit.stream(p, dry_run=True)
        out.append(seen_chunks[0])
    assert np.allclose(out[1], out[0] * 0.25)


def test_overdriven_stream_saturates_instead_of_wrapping(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_TX", True)
    p = transmit.TxParams(iq_path=_iq_file(tmp_path, samples=1000),
                          sample_rate=2.6e6, sample_format="int16",
                          chunk_samples=1000, level_dbfs=0.0, tx_scale=4.0)
    seen = []
    monkeypatch.setattr(transmit, "_DrySink", _recording_sink(seen))
    res = transmit.stream(p, dry_run=True)
    assert res["clipped"] > 0
    assert np.max(np.abs(seen[0].real)) <= 32767
    assert np.max(np.abs(seen[0].imag)) <= 32767


def test_rate_mismatch_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_TX", True)
    p = transmit.TxParams(iq_path=_iq_file(tmp_path), sample_rate=1.0e6,
                          sample_format="int16")
    with pytest.raises(transmit.TransmitError):
        transmit.stream(p, dry_run=True)  # 1.0 Msps below AD936x TX minimum


def test_stream_honors_cancel(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_TX", True)
    total_file_samples = 52000
    p = transmit.TxParams(iq_path=_iq_file(tmp_path, samples=total_file_samples),
                          sample_rate=2.6e6, sample_format="int16",
                          chunk_samples=13000)
    ev = threading.Event()
    ev.set()
    out = transmit.stream(p, dry_run=True, cancel=ev)
    assert out["samples"] < total_file_samples
    assert out["dry_run"] is True


def test_txsession_stop_ends_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_TX", True)
    p = transmit.TxParams(iq_path=_iq_file(tmp_path, samples=520000),
                          sample_rate=2.6e6, sample_format="int16",
                          chunk_samples=13000)
    session = transmit.TxSession(p, dry_run=True)
    seen = []

    def _drain():
        for d in session.start():
            seen.append(d)

    consumer = threading.Thread(target=_drain, daemon=True)
    consumer.start()
    time.sleep(0.05)
    session.stop()
    consumer.join(timeout=3.0)
    assert consumer.is_alive() is False
    assert session.running is False


def test_stream_uses_custom_chunk_source(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_TX", True)
    chunks = [np.array([1 + 1j, 2 + 2j], dtype=np.complex64),
             np.array([3 + 3j], dtype=np.complex64)]
    p = transmit.TxParams(iq_path="unused", sample_rate=2.6e6, sample_format="int16")
    seen = []
    monkeypatch.setattr(transmit, "_DrySink", _recording_sink(seen))
    out = transmit.stream(p, dry_run=True, chunk_source=iter(chunks))
    assert out["samples"] == 3
    assert len(seen) == 2


def test_default_baseband_offset_is_zero_and_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_TX", True)
    p = transmit.TxParams(iq_path=_iq_file(tmp_path, samples=1000),
                          sample_rate=2.6e6, sample_format="int16",
                          chunk_samples=1000)
    assert p.baseband_offset_hz == 0.0
    seen_chunks = []
    monkeypatch.setattr(transmit, "_DrySink", _recording_sink(seen_chunks))
    res = transmit.stream(p, dry_run=True)
    raw = next(transmit._iter_chunks(p.iq_path, p.sample_format, p.chunk_samples))
    # no rotation: output is the leveled input, sample for sample
    assert np.allclose(seen_chunks[0], raw * res["digital_scale"])


class _FakeAD9361ForCalibration:
    def __init__(self, uri=None):
        self.uri = uri
        self.tx_enabled_channels = None
        self.sample_rate = None
        self.tx_lo = None
        self.tx_cyclic_buffer = None
        self.tx_hardwaregain_chan0 = None
        self.tx_hardwaregain_chan1 = None
        self.destroyed = False

    def tx(self, blocks):
        time.sleep(0.001)

    def tx_destroy_buffer(self):
        self.destroyed = True


def test_nonzero_baseband_offset_triggers_a_calibration_attempt(tmp_path, monkeypatch):
    # A non-DISABLED LO offset moves the carrier off the AD9361's own TX LO
    # -- exactly the situation its internal TX quadrature calibration exists
    # for. calibrate() was written and unit-tested but never invoked from
    # any real transmit path; this proves stream() now actually calls it.
    monkeypatch.setattr(config, "ALLOW_TX", True)
    fake_module = types.ModuleType("adi")
    fake_module.ad9361 = _FakeAD9361ForCalibration
    monkeypatch.setitem(sys.modules, "adi", fake_module)
    dual_tx._card = None
    try:
        p = transmit.TxParams(
            iq_path=_iq_file(tmp_path, samples=1000), sample_rate=2.6e6,
            sample_format="int16", chunk_samples=1000,
            baseband_offset_hz=100_000.0, uri="ip:1.2.3.4")
        events = []
        transmit.stream(p, dry_run=False, progress_cb=events.append)
        cal_events = [d for d in events if "calibration" in d]
        assert cal_events, "calibrate() was never invoked from stream()"
        # The fake sdr has no _ctrl, so capabilities.detect() correctly
        # reports no TX_QUAD support -- proves calibrate() actually ran
        # against the real sdr handle, not a stub that always succeeds.
        assert cal_events[0]["calibration"]["success"] is False
        assert cal_events[0]["calibration"]["calibration_type"] == "NONE"
    finally:
        if dual_tx._card is not None:
            dual_tx._card.shut_down()
            dual_tx._card = None


def test_nonzero_baseband_offset_shifts_the_spectrum(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_TX", True)
    fs = 2_600_000.0
    n = 8192
    # A pure baseband tone at 0 Hz, written as raw int16 I/Q so
    # _iter_chunks reconstructs it as a real-valued-only (DC) complex signal.
    dtype = np.int16
    data = np.zeros(n * 2, dtype=dtype)
    data[0::2] = 10000  # I = constant, Q = 0 -> a DC (0 Hz) baseband tone
    p_path = tmp_path / "dc.bin"
    data.tofile(p_path)
    offset_hz = 100_000.0
    p = transmit.TxParams(iq_path=str(p_path), sample_rate=fs, sample_format="int16",
                          chunk_samples=n, baseband_offset_hz=offset_hz)
    seen_chunks = []
    monkeypatch.setattr(transmit, "_DrySink", _recording_sink(seen_chunks))
    transmit.stream(p, dry_run=True)
    spectrum = np.abs(np.fft.fft(seen_chunks[0]))
    peak_bin = np.argmax(spectrum)
    freqs = np.fft.fftfreq(n, d=1.0 / fs)
    assert abs(freqs[peak_bin] - offset_hz) < (fs / n) * 1.5
