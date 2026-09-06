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
                          start=dt.datetime(2026, 9, 1), duration_s=4,
                          sample_rate=6_000_000.0, sample_format="int16",
                          engine="native", systems=["G", "J", "E", "C", "S"])
    return engine.run(req)


@pytest.mark.xfail(reason="engine.run multi-system wiring in Task 16/17", strict=False)
def test_each_l1_system_acquires_in_one_correlated_capture(tmp_path, monkeypatch):
    import json

    from backend import inspector

    outdir = _run(tmp_path, monkeypatch, ("G", "J", "E", "C", "S"))
    meta = json.loads((outdir / "meta.json").read_text())
    l1 = next(b for b in meta["bands"] if b["id"] == "L1")
    iq = inspector.read_iq(outdir / l1["file"], "int16",
                           max_samples=int(l1["fs"] * 0.010))
    hits = {}
    for e in meta["provenance"]["svs"]:
        r = inspector.acquire(iq, l1["fs"], e["prn"],
                              code_len=e["code_len"], chip_hz=e["chip_hz"])
        hits.setdefault(e["sys"], 0)
        if r["metric_db"] > 11.0:
            hits[e["sys"]] += 1
    for s in ("G", "J", "E", "C", "S"):
        assert hits.get(s, 0) >= 1, (s, hits)


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
