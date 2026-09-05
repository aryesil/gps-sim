# backend/scenario_lib.py
"""Named scenario presets: save/load a channel's whole config (position,
timing, RF params) by name, mirroring backend/trajectory.py's save/load
pattern exactly -- same name validation, same JSON-on-disk storage, same
error type shape, just a different on-disk directory and payload."""
from __future__ import annotations

import json
import pathlib
import re

from backend import config

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DIR = config.DATA_DIR / "scenarios"

# Fields a scenario preset is allowed to carry. Kept as an explicit
# allowlist (rather than storing the raw request body) so a preset file
# can never smuggle in something like `iq_path` -- it holds simulation
# parameters only, not filesystem paths or a device URI.
_FIELDS = (
    "lat", "lon", "alt", "start_utc", "duration_s", "sample_rate",
    "sample_format", "rinex_path", "lo_hz", "tx_gain_db",
)


class ScenarioLibError(Exception):
    pass


def _path_for(name: str) -> pathlib.Path:
    if not _NAME_RE.match(name):
        raise ScenarioLibError(f"invalid scenario name {name!r}")
    return _DIR / f"{name}.json"


def save(name: str, params: dict) -> pathlib.Path:
    preset = {k: params[k] for k in _FIELDS if k in params}
    if not preset:
        raise ScenarioLibError("a scenario preset needs at least one field")
    _DIR.mkdir(parents=True, exist_ok=True)
    p = _path_for(name)
    p.write_text(json.dumps({"name": name, "params": preset}, indent=2))
    return p


def load(name: str) -> dict:
    p = _path_for(name)
    if not p.exists():
        raise ScenarioLibError(f"no saved scenario named {name!r}")
    data = json.loads(p.read_text())
    if "params" not in data or not isinstance(data["params"], dict):
        raise ScenarioLibError(f"corrupt scenario file for {name!r}")
    return data["params"]


def list_names() -> list[str]:
    if not _DIR.is_dir():
        return []
    return sorted(p.stem for p in _DIR.glob("*.json"))
