from __future__ import annotations

import pathlib

from backend import generator, scenario


def run(req: scenario.ScenarioRequest, progress_cb=None,
        binary: str | None = None) -> pathlib.Path:
    if req.engine == "gps-sdr-sim":
        if tuple(req.systems) != ("G",):
            raise ValueError("gps-sdr-sim engine is GPS-only")
        return generator.run(req, progress_cb=progress_cb, binary=binary)
    if req.engine == "native":
        from backend.synth import engine as native_engine
        return native_engine.run(req, progress_cb=progress_cb)
    raise ValueError(f"unknown engine {req.engine!r} (expected 'gps-sdr-sim' or 'native')")
