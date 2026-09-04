import threading
import time

import numpy as np
import pytest

from backend import transmit, config


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
