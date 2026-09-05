# backend/recording.py
"""Session recording & replay: capture the exact SSE payload sequence a
live transmit already emits (progress, spectrogram, cn0, timeline_step,
finished) to a JSONL file, then replay it later as the same SSE shape --
so the frontend needs no new message types to render a replay, just a
different source.

Recording is opt-in per live session (a channel's "Record" checkbox);
replay is read-only and never touches _tx_slots or real hardware -- it
is a pure playback of what was already recorded, at a chosen speed.
"""
from __future__ import annotations

import json
import pathlib
import time

from backend import config

_DIR_NAME = "recordings"


def _dir() -> pathlib.Path:
    d = config.OUT_DIR / _DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


class RecordingWriter:
    """One file per recorded session. append() is called once per SSE
    payload the live session already emits; each line gets a `t` field
    (seconds since the recording started) so replay can reproduce pacing."""

    def __init__(self, slot: str):
        self._started = time.monotonic()
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self.name = f"{slot}-{ts}"
        self.path = _dir() / f"{self.name}.jsonl"
        self._f = open(self.path, "w")

    def append(self, event: dict) -> None:
        row = {"t": time.monotonic() - self._started, **event}
        self._f.write(json.dumps(row) + "\n")
        self._f.flush()

    def close(self) -> None:
        try:
            self._f.close()
        except OSError:
            pass


def list_names() -> list[str]:
    return sorted(p.stem for p in _dir().glob("*.jsonl"))


def read_events(name: str) -> list[dict]:
    path = _dir() / f"{name}.jsonl"
    if not path.exists():
        raise FileNotFoundError(name)
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
