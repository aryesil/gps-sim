from __future__ import annotations

import json
import pathlib
import re

from backend import config

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DIR = config.DATA_DIR / "trajectories"


class TrajectoryError(Exception):
    pass


def _path_for(name: str) -> pathlib.Path:
    if not _NAME_RE.match(name):
        raise TrajectoryError(f"invalid trajectory name {name!r}")
    return _DIR / f"{name}.json"


def save(name: str, waypoints: list[dict]) -> pathlib.Path:
    if len(waypoints) < 2:
        raise TrajectoryError("a trajectory needs at least 2 waypoints")
    _DIR.mkdir(parents=True, exist_ok=True)
    p = _path_for(name)
    p.write_text(json.dumps({"name": name, "waypoints": waypoints}, indent=2))
    return p


def load(name: str) -> list[dict]:
    p = _path_for(name)
    if not p.exists():
        raise TrajectoryError(f"no saved trajectory named {name!r}")
    return json.loads(p.read_text())["waypoints"]


def list_names() -> list[str]:
    if not _DIR.is_dir():
        return []
    return sorted(p.stem for p in _DIR.glob("*.json"))
