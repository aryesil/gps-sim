"""meta.json carries enough provenance to reproduce a recording."""
import datetime as dt
import json
import pathlib
import stat
import textwrap

import pytest

from backend import generator, scenario
from backend.obs import provenance

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "brdc_sample.rnx"


def _fake_binary(tmp_path):
    p = tmp_path / "fake_sim.py"
    p.write_text(textwrap.dedent('''
        import sys
        out = sys.argv[sys.argv.index("-o") + 1]
        open(out, "wb").write(b"\\x01\\x02" * 500)
    '''))
    sh = tmp_path / "fake_sim"
    sh.write_text(f'#!/usr/bin/env bash\nexec python "{p}" "$@"\n')
    sh.chmod(sh.stat().st_mode | stat.S_IEXEC)
    return str(sh)


def _req(**kw):
    base = dict(rinex_path=str(FIX), lat=41.0, lon=29.0, alt=100.0,
                start=dt.datetime(2026, 8, 28, 12, 0, 0), duration_s=4)
    base.update(kw)
    return scenario.ScenarioRequest(**base)


# ---- provenance helpers ---------------------------------------------------

def test_scenario_hash_is_stable_and_sensitive():
    a = provenance.scenario_hash(_req())
    assert a == provenance.scenario_hash(_req())            # deterministic
    assert a != provenance.scenario_hash(_req(lat=41.001))  # position matters
    assert a != provenance.scenario_hash(_req(duration_s=5))
    assert a.startswith("sha256:")


def test_sha256_file_and_missing():
    h = provenance.sha256_file(FIX)
    assert h and h.startswith("sha256:") and len(h) == 71
    assert provenance.sha256_file("/no/such/file") is None


# ---- meta.json shape ----------------------------------------------------

def test_meta_records_hashes_versions_and_seed(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.config, "OUT_DIR", tmp_path)
    outdir = generator.run(_req(), binary=_fake_binary(tmp_path))
    meta = json.loads((outdir / "meta.json").read_text())

    p = meta["provenance"]
    assert p["scenario_hash"].startswith("sha256:")
    assert p["rinex_sha256"] == provenance.sha256_file(FIX)
    assert p["nav_sha256"] and p["nav_sha256"].startswith("sha256:")
    assert p["generator_version"].startswith("git:")
    assert "gps_sdr_sim_version" in p
    assert p["random_seed"] is None
    assert meta["config"]["ephemeris_mode"] == "broadcast"
    assert "precise" not in p


def test_meta_is_json_and_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.config, "OUT_DIR", tmp_path)
    outdir = generator.run(_req(), binary=_fake_binary(tmp_path))
    txt = (outdir / "meta.json").read_text()
    assert json.loads(txt) == json.loads(txt)
