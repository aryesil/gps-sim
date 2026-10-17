# tests/test_receiver.py
import numpy as np

from backend.analysis import receiver
from backend import config, geometry
from tests.synth.test_native_l1_multi_closed_loop_fix import (  # noqa: F401
    l1_multi_run,
)


def test_solve_position_recovers_known_point():
    rng = np.random.default_rng(1)
    true = np.array(geometry.llh_to_ecef(41.0, 29.0, 120.0))
    b_true = 3.5e-4
    up = true / np.linalg.norm(true)
    dirs = [[0.05, 0.02, 0.3], [0.6, -0.1, 0.2], [-0.5, 0.4, 0.15],
            [0.1, -0.6, 0.25], [-0.3, -0.35, 0.1], [0.45, 0.5, 0.05]]
    sats, pr = {}, {}
    for i, d in enumerate(dirs, start=1):
        v = up + np.array(d)
        s = v / np.linalg.norm(v) * 26.56e6
        sats[i] = s
        pr[i] = np.linalg.norm(s - true) + 299792458.0 * b_true + rng.normal(0, 3)
    out = receiver.solve_position(pr, sats)
    assert np.linalg.norm(np.array(out["ecef"]) - true) < 30.0
    assert abs(out["clock_bias_s"] - b_true) < 1e-7
    assert out["iterations"] < 10


def test_fix_from_iq_band_param_guard():
    import inspect
    import pytest

    sig = inspect.signature(receiver.fix_from_iq)
    p = sig.parameters["band"]
    assert p.default == "L1"
    assert p.kind is inspect.Parameter.KEYWORD_ONLY

    with pytest.raises(ValueError):
        receiver.fix_from_iq("/nonexistent.bin", "int8", 5e6, {}, 0.0,
                             band="L9")


def test_l5_blind_scan_acquires_nothing_on_pure_noise(tmp_path):
    # The largest of the ~3M noise-only acquisition cells sits ~13-15 dB
    # above the median; a threshold below that "acquires" every scanned
    # PRN and sends each one into a full nav demod.
    rng = np.random.default_rng(7)
    raw = np.clip(np.round(rng.standard_normal(2 * 75_000) * 20), -127, 127)
    path = tmp_path / "noise_l5.bin"
    raw.astype(np.int8).tofile(path)
    res = receiver.fix_from_iq(path, "int8", 25e6, {}, 0.0, band="L5")
    assert res["nav_decode"] == {}


def test_gps_only_unchanged(tmp_path):
    import inspect

    sig = inspect.signature(receiver.fix_from_iq)
    assert "systems" in sig.parameters
    assert sig.parameters["systems"].default is None

    rng = np.random.default_rng(42)
    raw = np.clip(np.round(rng.standard_normal(2 * 50_000) * 20), -127, 127)
    path = tmp_path / "noise_l1.bin"
    raw.astype(np.int8).tofile(path)

    res_none = receiver.fix_from_iq(path, "int8", 2.5e6, {}, 0.0, band="L1")
    res_g = receiver.fix_from_iq(path, "int8", 2.5e6, {}, 0.0, band="L1", systems=["G"])
    assert res_none == res_g
    assert res_g == {"error": "only 0 PRNs acquired", "prns_used": []}


def test_run_receiver_multi_endpoint(monkeypatch, l1_multi_run):
    from fastapi.testclient import TestClient
    from backend.app import app

    outdir, meta, _expected = l1_multi_run
    monkeypatch.setattr(config, "OUT_DIR", outdir.parent)
    client = TestClient(app)
    resp = client.post(
        "/api/receiver",
        json={"outdir": outdir.name, "marker": [45.0, 30.0, 100.0]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "error" not in data, f"receiver returned error: {data}"
    assert data["error_m"] < 30.0, f"error_m={data.get('error_m')}"


def test_standalone_inav_decode_fix(tmp_path, monkeypatch):
    """Verify Galileo E1 standalone fix with decode_nav=True and eph_by_prn={}."""
    import json
    import math
    from backend.synth import _lib, signals
    from backend.analysis import inav_encode, inav_decode

    fs = 16_368_000.0
    n_samples = int(fs * 0.020)
    rx_llh = (45.0, 30.0, 100.0)
    rx_ecef = np.array(geometry.llh_to_ecef(*rx_llh))
    sow = 561600.0
    sig = signals.SIGNALS["GAL_E1"]
    picks = [
        (1, 1.3227758541430708, 1.984163781214606),
        (2, 0.0, (4.29902152596498 + math.pi) % (2 * math.pi) - math.pi),
        (3, 2.3148577447503738, 0.6613879270715354),
        (4, 1.984163781214606, (6.283185307179586 + math.pi) % (2 * math.pi) - math.pi),
    ]
    ephs = {}
    t = np.arange(n_samples) / fs
    iq_total = np.zeros(n_samples, dtype=np.complex64)
    svs_meta = []

    for prn, m0, om0 in picks:
        eph = {
            "system": "E", "prn": prn, "iodnav": 1, "sqrtA": 5440.6, "e": 0.0002,
            "m0": m0, "toe": sow, "toc": sow, "omega0": om0, "i0": math.radians(56.0),
            "omega": 0.5, "idot": 0.0, "omega_dot": -5e-9, "delta_n": 3e-9,
            "cuc": 0.0, "cus": 0.0, "crc": 0.0, "crs": 0.0, "cic": 0.0, "cis": 0.0,
            "af0": 0.0, "af1": 0.0, "af2": 0.0, "tgd": 0.0, "tgd_e5a": 0.0,
            "gal_week": 1325, "tow": sow,
        }
        ephs[prn] = eph
        o = geometry.observables(eph, rx_ecef, sow, signal=sig)
        pred_phase = (o["pseudorange_m"] / config.C * 1.023e6) % 4092
        chip_phase = (t * (1.023e6 + o["code_doppler_hz"]) + (4092 - pred_phase)) % 4092
        c_idx = np.floor(chip_phase).astype(int)
        c_raw = _lib.code(6, prn, 4092, 0)[0]
        code = c_raw[c_idx].astype(np.float32)
        sub = np.where(np.floor(2.0 * chip_phase).astype(np.int64) & 1, -1.0, 1.0).astype(np.float32)
        fd = o["carrier_doppler_hz"]
        carr = np.exp(2j * np.pi * fd * t).astype(np.complex64)
        iq_total += (code * sub).astype(np.complex64) * carr
        svs_meta.append({
            "sys": "E", "prn": prn, "band": "L1", "code_len": 4092, "chip_hz": 1.023e6,
            "if_hz": 0.0, "code_doppler_hz": o["code_doppler_hz"], "el_deg": o["el_deg"], "az_deg": o["az_deg"],
        })

    inav_syms_by_prn = {}
    for prn, eph in ephs.items():
        stream = inav_encode.nav_stream(eph, 1325, sow, 12.0)
        inav_syms_by_prn[prn] = [1 if s > 0 else 0 for s in stream]

    raw = np.empty(2 * n_samples, dtype=np.int16)
    raw[0::2] = np.clip(np.round(iq_total.real * 4000), -32767, 32767).astype(np.int16)
    raw[1::2] = np.clip(np.round(iq_total.imag * 4000), -32767, 32767).astype(np.int16)
    iq_path = tmp_path / "gpssim.bin"
    raw.tofile(iq_path)

    meta = {
        "sample_format": "int16",
        "sample_rate": fs,
        "systems": ["E"],
        "config": {"lat": 45.0, "lon": 30.0, "alt": 100.0, "start_utc": "2026-09-01T12:00:00"},
        "provenance": {"svs": svs_meta},
        "bands": [{"id": "L1", "file": "gpssim.bin", "fs": fs}],
    }
    (tmp_path / "meta.json").write_text(json.dumps(meta))

    monkeypatch.setattr(
        inav_decode,
        "demod_symbols_e1b",
        lambda iq, fs, prn, **kw: inav_syms_by_prn[prn],
    )

    res = receiver.fix_from_iq(iq_path, "int16", fs, {}, sow, marker_llh=rx_llh, decode_nav=True)
    assert "error" not in res, f"Receiver fix failed: {res}"
    assert res["error_m"] < 30.0, f"Error too large: {res['error_m']} m"
    assert set(res["prns_used"]) == {"E1", "E2", "E3", "E4"}


def test_standalone_multi_gnss_l1_decode_fix(tmp_path, monkeypatch):
    """Verify joint GPS + Galileo + BeiDou fix with decode_nav=True and eph_by_prn={}."""
    import json
    import math
    from backend.synth import _lib, signals
    from backend.analysis import (
        inav_encode, inav_decode,
        lnav_encode, lnav_decode,
        bds_d1_encode, bds_d1_decode,
    )

    fs = 16_368_000.0
    n_samples = int(fs * 0.020)
    rx_llh = (45.0, 30.0, 100.0)
    rx_ecef = np.array(geometry.llh_to_ecef(*rx_llh))
    sow = 561600.0
    t = np.arange(n_samples) / fs

    configs = [
        ("G", 1, 1.32, 1.98, signals.SIGNALS["GPS_L1CA"], 0, 1023, 1.023e6, False),
        ("G", 2, 0.0, (4.3 + math.pi) % (2 * math.pi) - math.pi, signals.SIGNALS["GPS_L1CA"], 0, 1023, 1.023e6, False),
        ("E", 1, 2.31, 0.66, signals.SIGNALS["GAL_E1"], 6, 4092, 1.023e6, True),
        ("E", 2, 1.98, (6.28 + math.pi) % (2 * math.pi) - math.pi, signals.SIGNALS["GAL_E1"], 6, 4092, 1.023e6, True),
        ("C", 6, 0.5, 1.2, signals.SIGNALS["BDS_B1I"], 3, 2046, 2.046e6, False),
        ("C", 7, 2.0, 2.5, signals.SIGNALS["BDS_B1I"], 3, 2046, 2.046e6, False),
    ]

    ephs = {}
    iq_total = np.zeros(n_samples, dtype=np.complex64)
    svs_meta = []

    for sysc, prn, m0, om0, sig, enum, code_len, chip_hz, is_boc in configs:
        eph = {
            "system": sysc, "prn": prn, "iodnav": 1,
            "sqrtA": 5440.6 if sysc == "E" else (5153.7 if sysc == "G" else 5282.0),
            "e": 0.0002, "m0": m0, "toe": sow, "toc": sow, "omega0": om0,
            "i0": math.radians(56.0 if sysc == "E" else 55.0),
            "omega": 0.5, "idot": 0.0, "omega_dot": -5e-9, "delta_n": 3e-9,
            "cuc": 0.0, "cus": 0.0, "crc": 0.0, "crs": 0.0, "cic": 0.0, "cis": 0.0,
            "af0": 0.0, "af1": 0.0, "af2": 0.0, "tgd": 0.0, "tgd_e5a": 0.0,
            "gal_week": 1325, "gps_week": 2325, "bdt_week": 850, "tow": sow,
        }
        ephs[(sysc, prn)] = eph
        o = geometry.observables(eph, rx_ecef, sow, signal=sig)
        pred_phase = (o["pseudorange_m"] / config.C * chip_hz) % code_len
        chip_phase = (t * (chip_hz + o["code_doppler_hz"]) + (code_len - pred_phase)) % code_len
        c_idx = np.floor(chip_phase).astype(int)
        c_raw = _lib.code(enum, prn, code_len, 0)[0]
        code = c_raw[c_idx].astype(np.float32)
        if is_boc:
            sub = np.where(np.floor(2.0 * chip_phase).astype(np.int64) & 1, -1.0, 1.0).astype(np.float32)
        else:
            sub = 1.0
        fd = o["carrier_doppler_hz"]
        carr = np.exp(2j * np.pi * fd * t).astype(np.complex64)
        iq_total += (code * sub).astype(np.complex64) * carr
        svs_meta.append({
            "sys": sysc, "prn": prn, "band": "L1", "code_len": code_len, "chip_hz": chip_hz,
            "if_hz": 0.0, "code_doppler_hz": o["code_doppler_hz"], "el_deg": o["el_deg"], "az_deg": o["az_deg"],
        })

    hdr = {
        "iono_alpha": [1.1e-8, 0.0, -5.96e-8, 0.0],
        "iono_beta": [88064.0, 0.0, -196608.0, 0.0],
        "utc": {"A0": 1.86e-9, "A1": 3.5e-15},
    }
    g_bits = {}
    for p in (1, 2):
        g_bits[p] = lnav_encode.frame_bits(ephs[("G", p)], hdr, 2325, int(sow // 6), 1)

    e_syms = {}
    for p in (1, 2):
        st = inav_encode.nav_stream(ephs[("E", p)], 1325, sow, 12.0)
        e_syms[p] = [1 if s > 0 else 0 for s in st]

    c_syms = {}
    for p in (6, 7):
        st, _ = bds_d1_encode.nav_stream(ephs[("C", p)], 850, sow, 12.0)
        c_syms[p] = [1 if s > 0 else 0 for s in st]

    raw = np.empty(2 * n_samples, dtype=np.int16)
    raw[0::2] = np.clip(np.round(iq_total.real * 3000), -32767, 32767).astype(np.int16)
    raw[1::2] = np.clip(np.round(iq_total.imag * 3000), -32767, 32767).astype(np.int16)
    iq_path = tmp_path / "gpssim.bin"
    raw.tofile(iq_path)

    meta = {
        "sample_format": "int16",
        "sample_rate": fs,
        "systems": ["G", "E", "C"],
        "config": {"lat": 45.0, "lon": 30.0, "alt": 100.0, "start_utc": "2026-09-01T12:00:00"},
        "provenance": {"svs": svs_meta},
        "bands": [{"id": "L1", "file": "gpssim.bin", "fs": fs}],
    }
    (tmp_path / "meta.json").write_text(json.dumps(meta))

    monkeypatch.setattr(
        lnav_decode,
        "demod_nav_bits",
        lambda iq, fs, prn, cp, dopp, **kw: g_bits[prn],
    )
    monkeypatch.setattr(
        inav_decode,
        "demod_symbols_e1b",
        lambda iq, fs, prn, **kw: e_syms[prn],
    )
    monkeypatch.setattr(
        bds_d1_decode,
        "demod_symbols_b1i",
        lambda iq, fs, prn, **kw: c_syms[prn],
    )

    res = receiver.fix_from_iq(iq_path, "int16", fs, {}, sow, marker_llh=rx_llh, decode_nav=True)
    assert "error" not in res, f"Receiver fix failed: {res}"
    assert res["error_m"] < 30.0, f"Error too large: {res['error_m']} m"
    assert any(p.startswith("G") for p in res["prns_used"])
    assert any(p.startswith("E") for p in res["prns_used"])
    assert any(p.startswith("C") for p in res["prns_used"])



