import datetime as dt
import pathlib

from backend import config
from backend.scenario import ScenarioRequest
from backend.synth import engine

_MIXED = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_mixed.rnx")


def _req(outdir, **kw):
    base = dict(rinex_path=_MIXED, lat=41.0, lon=29.0, alt=100.0,
               start=dt.datetime(2026, 9, 1, 12), duration_s=2,
               sample_rate=6_000_000.0, sample_format="int16",
               engine="native", systems=["G"])
    base.update(kw)
    return ScenarioRequest(**base)


def test_bands_unset_matches_bands_l1_list(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    a = engine.run(_req(tmp_path / "a"))
    b = engine.run(_req(tmp_path / "b", bands=["L1"]))
    assert (a / "gpssim.bin").read_bytes() == (b / "gpssim.bin").read_bytes()


def test_bands_unset_emits_only_gpssim_bin(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    out = engine.run(_req(tmp_path / "a"))
    assert (out / "gpssim.bin").exists()
    assert not (out / "gpssim_l2.bin").exists()
    assert not (out / "gpssim_l5.bin").exists()
