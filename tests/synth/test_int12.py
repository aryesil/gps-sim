import datetime as dt
import pathlib

import numpy as np

from backend import config, inspector, scenario
from backend import synth
from backend.synth import engine

_RINEX = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_full.rnx")


def test_int12_output_is_int16_container_bounded_2047(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    req = scenario.ScenarioRequest(
        rinex_path=_RINEX, lat=41.0, lon=29.0, alt=100.0,
        start=dt.datetime(2024, 1, 1), duration_s=3,
        sample_rate=2_600_000.0, sample_format="int12", engine="native")
    outdir = engine.run(req)
    raw = np.fromfile(outdir / "gpssim.bin", dtype=np.int16)
    assert raw.size > 0
    assert np.abs(raw).max() <= 2047
    # still acquires
    iq = inspector.read_iq(outdir / "gpssim.bin", synth.container_format("int12"),
                           max_samples=int(2_600_000.0 * 0.010))
    hits = sum(1 for prn in range(1, 33)
               if inspector.acquire(iq, 2_600_000.0, prn)["metric_db"] > 12.0)
    assert hits >= 4
