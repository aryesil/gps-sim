# tests/test_iqplot_scrub.py
import json

import numpy as np
from fastapi.testclient import TestClient

from backend import config, inspector
from backend.app import app

client = TestClient(app)


def _write_fake_capture(tmp_path, n_samples=20000, sample_rate=2_600_000.0):
    outdir = tmp_path / "run1"
    outdir.mkdir()
    rng = np.random.default_rng(0)
    interleaved = rng.integers(-100, 100, 2 * n_samples).astype(np.int16)
    interleaved.tofile(outdir / "gpssim.bin")
    (outdir / "meta.json").write_text(json.dumps(
        {"sample_format": "int16", "sample_rate": sample_rate}))
    return outdir


def test_iq_sample_count_matches_written_samples(tmp_path):
    outdir = _write_fake_capture(tmp_path, n_samples=12345)
    assert inspector.iq_sample_count(outdir / "gpssim.bin", "int16") == 12345


def test_read_iq_offset_returns_later_window(tmp_path):
    outdir = _write_fake_capture(tmp_path, n_samples=5000)
    whole = inspector.read_iq(outdir / "gpssim.bin", "int16")
    windowed = inspector.read_iq(outdir / "gpssim.bin", "int16",
                                  max_samples=100, offset_samples=200)
    assert np.array_equal(windowed, whole[200:300])


def _write_fake_multiband(tmp_path, n_samples=20000):
    outdir = tmp_path / "run1"
    outdir.mkdir()
    rng = np.random.default_rng(1)
    for fname in ("gpssim.bin", "gpssim_g1.bin"):
        interleaved = rng.integers(-100, 100, 2 * n_samples).astype(np.int16)
        interleaved.tofile(outdir / fname)
    (outdir / "meta.json").write_text(json.dumps({
        "sample_format": "int16", "sample_rate": 2_600_000.0,
        "bands": [
            {"id": "L1", "centre_hz": 1575420000.0, "fs": 2_600_000.0,
             "file": "gpssim.bin", "systems": ["G", "E"]},
            {"id": "G1", "centre_hz": 1602000000.0, "fs": 5_000_000.0,
             "file": "gpssim_g1.bin", "systems": ["R"]},
        ],
    }))
    return outdir


def test_api_iqplot_multiband_breakdown(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    _write_fake_multiband(tmp_path, n_samples=20000)
    r = client.get("/api/iqplot?outdir=run1&n=500")
    assert r.status_code == 200
    d = r.json()
    assert len(d["bands"]) == 2
    assert [b["centre_hz"] for b in d["bands"]] == [1575420000.0, 1602000000.0]
    for b in d["bands"]:
        assert b["spectrum_db"]
        assert b["total_samples"] == 20000
    # legacy top-level keys mirror the L1 band
    assert d["i"] == d["bands"][0]["i"]
    assert d["spectrum_db"] == d["bands"][0]["spectrum_db"]
    assert d["total_samples"] == 20000
    assert d["sample_format"] == "int16"


def test_api_iqplot_reports_total_samples_and_honors_offset(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    _write_fake_capture(tmp_path, n_samples=20000)
    r = client.get("/api/iqplot?outdir=run1&n=500&offset=1000")
    assert r.status_code == 200
    d = r.json()
    assert d["total_samples"] == 20000
    assert d["offset"] == 1000
    assert d["sample_format"] == "int16"     # scrubber needs it for a dBFS readout
    assert len(d["i"]) == 500
