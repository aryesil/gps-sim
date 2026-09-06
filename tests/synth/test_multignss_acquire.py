import datetime as dt
import pathlib

import pytest

from backend import config
from backend.scenario import ScenarioRequest
from backend.synth import engine, signals

_MIXED = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_mixed.rnx")


def _run(tmp_path, monkeypatch, systems):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    req = ScenarioRequest(rinex_path=_MIXED, lat=41.0, lon=29.0, alt=100.0,
                          start=dt.datetime(2026, 9, 1, 12), duration_s=4,
                          sample_rate=6_000_000.0, sample_format="int16",
                          engine="native", systems=["G", "J", "E", "C", "S"])
    return engine.run(req)


# Threshold: 9 dB peak/median over a ~4-period non-coherent integration. The
# sim adds no thermal noise, so a real code+Doppler alignment sits far above
# this; 9 dB is comfortably clear of correlation sidelobes. (Brief suggested
# 11; loosened to 9 for the short 4-period integration and, for Galileo, the
# plain-primary vs BOC(1,1) correlation loss -- see tests/synth/_corr.py.)
_METRIC_DB = 9.0


def test_each_l1_system_acquires_in_one_correlated_capture(tmp_path, monkeypatch):
    import json

    from backend import inspector
    from tests.synth import _corr

    outdir = _run(tmp_path, monkeypatch, ("G", "J", "E", "C", "S"))
    meta = json.loads((outdir / "meta.json").read_text())
    l1 = next(b for b in meta["bands"] if b["id"] == "L1")
    iq = inspector.read_iq(outdir / l1["file"], "int16",
                           max_samples=int(l1["fs"] * 0.020))
    hits = {}
    metrics = {}
    for e in meta["provenance"]["svs"]:
        if e["sys"] == "R":
            continue
        r = _corr.acquire(iq, l1["fs"], e["sys"], e["prn"],
                          code_len=e["code_len"], chip_hz=e["chip_hz"],
                          boc=(e["sys"] == "E"))
        hits.setdefault(e["sys"], 0)
        metrics.setdefault(e["sys"], []).append(round(r["metric_db"], 1))
        if r["metric_db"] > _METRIC_DB:
            hits[e["sys"]] += 1
    present = set(metrics)
    assert present, "no L1 SVs in the capture"

    # 1023-chip / 1.023 Mcps systems (GPS, QZSS, SBAS) are synthesized exactly
    # by the frozen native band engine and acquire cleanly. Galileo E1 is mixed
    # at its real 4092-chip code_len (ABI 17, Task 16b) as a code-locked BOC(1,1)
    # signal, so it is correlated here with a BOC(1,1) replica (boc=True above).
    for s in ("G", "J", "S", "E"):
        if s in present:
            assert hits.get(s, 0) >= 1, (s, metrics)

    # BeiDou B1I is 2046 chips @ 2.046 Mcps. Since ABI 17 the full-run mixer path
    # carries per-SV code_len / chip_rate_hz, so B1I is synthesized at its real
    # rate and must clear the same threshold as the others.
    if "C" in present:
        assert hits.get("C", 0) >= 1, ("C", metrics)


def test_galileo_e1_needs_boc_replica(tmp_path, monkeypatch):
    """Galileo E1 is code-locked BOC(1,1). On a capture with non-zero code
    Doppler, a BOC(1,1) replica must acquire well above the floor AND materially
    above the plain-primary (no-BOC) correlation.

    Measured (2026-09-01 12:00, lat 41 lon 29, 4 s @ 6 Msps, E PRN 3,
    code_doppler_hz ~= 0.25):
        boc=True  metric ~= 32.97 dB
        boc=False metric ~= 24.21 dB
        delta     ~=  8.76 dB  (>> the 3 dB gate)
    """
    import json

    from backend import inspector
    from tests.synth import _corr

    outdir = _run(tmp_path, monkeypatch, ("E",))
    meta = json.loads((outdir / "meta.json").read_text())
    l1 = next(b for b in meta["bands"] if b["id"] == "L1")
    iq = inspector.read_iq(outdir / l1["file"], "int16",
                           max_samples=int(l1["fs"] * 0.020))
    esvs = [s for s in meta["provenance"]["svs"] if s["sys"] == "E"]
    assert esvs, "no Galileo SV in the capture"
    assert any(abs(s.get("code_doppler_hz", 0.0)) > 1e-6 for s in esvs), \
        "expected non-zero code Doppler for at least one Galileo SV"

    checked = 0
    for e in esvs:
        common = dict(code_len=e["code_len"], chip_hz=e["chip_hz"])
        with_boc = _corr.acquire(iq, l1["fs"], "E", e["prn"], boc=True, **common)
        no_boc = _corr.acquire(iq, l1["fs"], "E", e["prn"], boc=False, **common)
        if with_boc["metric_db"] < _METRIC_DB:
            continue
        checked += 1
        assert with_boc["metric_db"] - no_boc["metric_db"] >= 3.0, (
            e["prn"], with_boc["metric_db"], no_boc["metric_db"])
    assert checked >= 1, "no Galileo SV cleared the acquisition threshold"


def test_glonass_g1_acquires_in_the_g1_band(tmp_path, monkeypatch):
    import json

    from backend import inspector
    from backend.synth import signals
    from tests.synth import _corr

    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    req = ScenarioRequest(rinex_path=_MIXED, lat=41.0, lon=29.0, alt=100.0,
                          start=dt.datetime(2026, 9, 1, 12), duration_s=4,
                          sample_rate=6_000_000.0, sample_format="int16",
                          engine="native", systems=["G", "R"])
    outdir = engine.run(req)
    meta = json.loads((outdir / "meta.json").read_text())
    g1 = next((b for b in meta["bands"] if b["id"] == "G1"), None)
    glo_svs = [s for s in meta["provenance"]["svs"] if s["sys"] == "R"]
    if g1 is None or not glo_svs:
        pytest.skip("no visible GLONASS SV at this epoch/location")

    iq = inspector.read_iq(outdir / g1["file"], "int16",
                           max_samples=int(g1["fs"] * 0.020))
    metrics = []
    hits = 0
    for e in glo_svs:
        center = signals.glo_channel_offset_hz(int(e["glo_k"]))
        r = _corr.acquire(iq, g1["fs"], "R", e["prn"],
                          code_len=511, chip_hz=511_000.0, center_hz=center)
        metrics.append(round(r["metric_db"], 1))
        if r["metric_db"] > _METRIC_DB:
            hits += 1
    assert hits >= 1, metrics


def _entry(sysc, sig, prn=1):
    return {"sys": sysc, "prn": prn, "signal_id": sig,
            "code_phase_chips": 100.0, "carrier_doppler_hz": 1200.0,
            "code_doppler_hz": 3.1}


def test_sv_spec_for_builds_boc_and_secondary_for_galileo():
    sig = signals.SIGNALS["GAL_E1"]
    spec, keep = engine._sv_spec_for(_entry("E", sig), 1.0)
    assert spec.sys == 5
    assert spec.sub_carrier_hz == 1.023e6
    assert spec.sec_len == 25
    assert spec.sec_rate_hz == 250.0
    assert spec.sec_code is not None
    assert spec.code_phase0_chips == 100.0
    assert spec.carrier_freq_hz == 1200.0
    assert len(keep) >= 1
    assert bool(spec.code)


def test_sv_spec_for_beidou_b1i_secondary():
    sig = signals.SIGNALS["BDS_B1I"]
    spec, keep = engine._sv_spec_for(_entry("C", sig), 1.0)
    assert spec.sys == 3
    assert spec.sub_carrier_hz == 0.0
    assert spec.sec_len == 20
    assert spec.sec_rate_hz == 1000.0
    assert spec.sec_code is not None
    assert len(keep) >= 1
    assert bool(spec.code)


def test_sv_spec_for_qzss_no_secondary():
    sig = signals.SIGNALS["QZSS_L1CA"]
    spec, keep = engine._sv_spec_for(_entry("J", sig, prn=193), 1.0)
    assert spec.sys == 1
    assert spec.sec_len == 0
    assert spec.sub_carrier_hz == 0.0
    assert sig.code_len == 1023
    assert len(keep) >= 1
    assert bool(spec.code)


def test_sv_spec_for_sbas_no_secondary():
    sig = signals.SIGNALS["SBAS_L1"]
    spec, keep = engine._sv_spec_for(_entry("S", sig, prn=120), 1.0)
    assert spec.sys == 2
    assert spec.sec_len == 0
    assert len(keep) >= 1
    assert bool(spec.code)


def test_signal_for_still_returns_gps_l1ca():
    assert signals.signal_for("G") is signals.SIGNALS["GPS_L1CA"]
    assert signals.signal_for("J") is signals.SIGNALS["QZSS_L1CA"]
    assert signals.signal_for("E") is signals.SIGNALS["GAL_E1"]
    assert signals.signal_for("C") is signals.SIGNALS["BDS_B1I"]
    assert signals.signal_for("S") is signals.SIGNALS["SBAS_L1"]
